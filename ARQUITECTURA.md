# XookHub — Documentación técnica del backend

> Guía de arquitectura y flujos de trabajo del monolito modular en `src/`. Pensada para alguien que se une al proyecto y necesita entender cómo viaja una petición de punta a punta.

## Tabla de contenidos

1. [Qué es XookHub](#1-qué-es-xookhub)
2. [Cómo está organizado el código](#2-cómo-está-organizado-el-código)
3. [El punto de entrada: `main.py`](#3-el-punto-de-entrada-mainpy)
4. [Autenticación: cómo entra un usuario](#4-autenticación-cómo-entra-un-usuario)
5. [Salas: el límite de tenencia (`rooms`)](#5-salas-el-límite-de-tenencia-rooms)
6. [Documentos: el pipeline de ingesta](#6-documentos-el-pipeline-de-ingesta)
7. [RAG: cómo se responde una pregunta](#7-rag-cómo-se-responde-una-pregunta)
8. [Generación de material de estudio](#8-generación-de-material-de-estudio)
9. [Infraestructura](#9-infraestructura)

---

## 1. Qué es XookHub

Un **monolito modular** en FastAPI: una plataforma de estudio con salas colaborativas (`rooms`), donde subes documentos, la IA los indexa, y luego puedes chatear con ellos (RAG), generar resúmenes/flashcards/exámenes, y estudiar con repetición espaciada (SM-2).

**Stack:** Postgres + pgvector · Redis · RabbitMQ + Celery · MinIO · Gemini (vía `google-genai`) · Docker Compose + Nginx como gateway.

## 2. Cómo está organizado el código

No hay una separación por capas técnicas a lo ancho de todo el proyecto (no existe una carpeta `controllers/` global, otra `models/` global, etc.). La separación es por **dominio**:

```
src/
├── users/        # identidad, perfiles, API keys
├── rooms/        # tenencia multi-usuario, roles, chat de sala
├── documents/    # subida, storage en MinIO, parsing, chunking
├── rag/          # embeddings, retrieval, conversaciones, streaming
├── generation/   # resúmenes, flashcards (SM-2), exámenes
├── worker/       # Celery: tareas asíncronas
└── core/         # excepciones, respuestas, seguridad transversales
```

Cada módulo de dominio repite siempre el mismo patrón de 4 archivos:

| Archivo | Responsabilidad |
|---|---|
| `models.py` | Tablas SQLAlchemy |
| `schemas.py` | Contratos Pydantic (entrada/salida) |
| `router.py` | Endpoints FastAPI — solo orquesta, no decide |
| `service.py` | Lógica de negocio real, **agnóstica de FastAPI** |

Esto es clave: los `service.py` nunca importan nada de FastAPI. Son testeables como Python puro y reutilizables desde Celery — de hecho, el worker instancia `GenerationService` y `RAGService` exactamente igual que un router.

## 3. El punto de entrada: `main.py`

Al arrancar el proceso, `main.py` hace tres cosas antes de levantar nada:

1. **Importa todos los `models.py`** de cada módulo, aunque no los use directamente. Es necesario porque SQLAlchemy resuelve relaciones declaradas como strings (`relationship("StudyRoom")`) de forma perezosa, y necesita que la clase ya esté registrada en el mapper registry antes de la primera query.
2. **Registra los exception handlers globales** (`core/exceptions.py`).
3. **Monta cada router** bajo `/api/v1/...`.

Cada respuesta exitosa sale envuelta en un sobre estándar:

```json
{ "data": { ... }, "meta": null, "error": null }
```

Cada excepción de negocio (`AppException` y sus hijas — `NotFoundException`, `ConflictException`, `AuthorizationException`, `ValidationException`) se traduce automáticamente al mismo formato, con su propio `status_code` y `code`. El frontend nunca tiene que lidiar con el shape default de FastAPI (`{"detail": "..."}"`) ni distinguir un 404 de negocio de un error de validación.

## 4. Autenticación: cómo entra un usuario

No hay login propio: la autenticación vive en **Supabase Auth**, y este backend solo *verifica* el JWT que el frontend ya trae en cada request.

Flujo en `core/security.py` → `verify_supabase_jwt`:

1. Llega un Bearer token.
2. Se lee el header **sin verificar** para saber el algoritmo (`alg`).
3. Si es `ES256`/`RS256` (caso normal): se busca la clave pública en el JWKS de Supabase (`https://<proyecto>.supabase.co/auth/v1/.well-known/jwks.json`), cacheada 10 minutos vía `PyJWKClient`.
4. Si es `HS256` (modo legado, solo durante una rotación de claves): se verifica contra `SUPABASE_JWT_SECRET`.
5. Se extrae el `sub` del payload — el UUID del usuario.

**Detalle fino — lazy user sync:** Supabase debería llamar a un webhook `POST /users/sync` justo después del signup para crear la fila en `users`. Pero como ese webhook es fácil de olvidar configurar en el dashboard, `verify_supabase_jwt` **siempre** ejecuta `_ensure_user_row`: si la fila no existe, la crea con un INSERT idempotente (`ON CONFLICT DO NOTHING`). Esto evita que cualquier FK hacia `users.id` (memberships, documentos, flashcards...) explote con un 500 confuso la primera vez que alguien usa la API. El webhook sigue siendo el "fast path"; esto es la red de seguridad.

## 5. Salas: el límite de tenencia (`rooms`)

`StudyRoom` es la frontera multi-tenant de toda la app: documentos, conversaciones, flashcards y exámenes cuelgan de una sala. El control de acceso pasa **siempre** por un único punto de choque: `RoomService.require_role(room_id, user_id, minimo)`.

Cuatro roles con jerarquía:

```
VIEWER  <  MEMBER  <  ADMIN  <  OWNER
(leer)     (participar)  (gestionar miembros)  (todo, incl. borrar sala)
```

**Decisión de diseño deliberada:** si el usuario **no es miembro** de la sala, se lanza `NotFoundException` (404), no `AuthorizationException` (403). Así no se filtra si una sala privada existe o no a alguien ajeno a ella — evita usar el status code como un oráculo de existencia.

Flujos principales:

- **Crear sala** → el creador queda automáticamente como `OWNER`.
- **Unirse por código** → cada sala puede generar un código de 6 caracteres (alfabeto sin caracteres ambiguos: sin `0/O`, `1/I/L`) que se canjea en `POST /rooms/join`. Regenerar el código invalida el anterior — como "resetear el link de invitación".
- **Chat comunitario** (`GroupMessage`) → al postear, se hace `commit()` **antes** de notificar. La notificación en tiempo real se dispara vía Supabase Realtime Broadcast, de forma best-effort: si Realtime está caído, el mensaje ya quedó guardado y el cliente lo verá en su próximo fetch, en vez de fallar toda la request por un canal de notificación.

## 6. Documentos: el pipeline de ingesta

El flujo más orquestado del sistema, en tres fases.

### Fase 1 — Subida (síncrona, dentro del request)

`DocumentService.upload()`:
- Valida el tipo de archivo: allowlist de MIME types + allowlist de extensiones de código (`.py`, `.js`, `.go`...) + blocklist explícita de ejecutables (`.exe`, `.dll`, `.sh` binarios, etc.).
- Rechaza binarios disfrazados de texto: si el archivo dice ser `.py` pero no decodifica como UTF-8, se rechaza ahí mismo en vez de fallar confusamente después, al parsear.
- Guarda el archivo en MinIO bajo `rooms/{room_id}/{document_id}/{filename}`.
- Crea la fila `Document` con estado `PENDING`.

### Fase 2 — El orden del commit importa

El router hace `db.commit()` **antes** de llamar `process_document_task.delay(...)`. La razón: Celery corre en un proceso worker separado, con su propia conexión a la base de datos. Si la tarea se encolara antes de que la fila esté durablemente comiteada, el worker podría consultar por `document_id` y no encontrar nada todavía. Este mismo patrón (commit → luego disparar efecto externo) se repite para el chat grupal y para la generación de exámenes de sala.

### Fase 3 — El worker procesa (asíncrono)

`worker/tasks.py` tiene una decisión de arquitectura no trivial: en vez de crear un event loop nuevo por tarea (`asyncio.run()`), mantiene **un solo event loop persistente por proceso worker**, corriendo en un thread dedicado.

¿Por qué? El pool de conexiones de `asyncpg` queda atado al loop donde se creó. Si cada tarea destruye su loop al terminar, la siguiente tarea hereda conexiones "huérfanas" del pool y explota con `RuntimeError: Event loop is closed`. La solución: un loop que vive toda la vida del proceso worker; cada tarea somete su corrutina ahí vía `asyncio.run_coroutine_threadsafe(...)`.

El pipeline real de `_process_document`:

```
PENDING → PROCESSING → (descarga MinIO → parse → chunk → embed) → INDEXED
                                                              └─→ FAILED (si algo truena)
                                                              └─→ QUARANTINED (mime no soportado)
```

1. `PENDING → PROCESSING`, commit inmediato — para que el polling de `GET /documents/{id}/status` muestre progreso ya.
2. Descarga el archivo de MinIO a un tmp local.
3. Parsea según el mime type — patrón **Strategy** en `parser.py`: `PlainTextParser`, `PDFParser` (pypdf), `DocxParser` (python-docx), `CodeParser`. Cada uno devuelve una lista de `ParsedPage`.
4. Trocea el texto (`chunk_text`) en fragmentos de ~1000 caracteres con 150 de overlap.
5. Genera los embeddings **en un solo batch** (no chunk por chunk) — muchas menos idas y vueltas al proveedor.
6. Persiste todos los `DocumentChunk` + marca `INDEXED`, todo en **una sola transacción atómica** — nunca se ve un set de chunks parcialmente escrito.

Si el embedding falla, degrada con gracia: guarda el chunk con `embedding=None` en vez de tirar todo el documento a `FAILED` — un job de reconciliación futuro podría rellenar los vectores faltantes.

## 7. RAG: cómo se responde una pregunta

Hay **dos caminos** distintos para chatear con los documentos de una sala.

### A) Quick answer — `POST /rooms/{id}/chat`

Sin estado, sin historial. Para una pregunta suelta:

1. Embebe la query.
2. Recupera los 5 chunks más cercanos por **distancia coseno de pgvector** (operador `<=>`), filtrados **siempre** por `room_id` — esa es la garantía de aislamiento multi-tenant en RAG. Sin ese filtro, cualquiera podría leer chunks de otra sala.
3. Arma el prompt y devuelve la respuesta completa. Nada se persiste.

### B) Conversación con streaming — `POST /conversations/{id}/messages`

Con historial persistente, respondiendo vía **Server-Sent Events**. Es la única ruta del proyecto que no usa la dependencia `get_db` de request-scope, porque el streaming sobrevive más allá del `return` del handler — abre su propia sesión de BD que gestiona su propio commit al final del generador.

```
usuario pregunta
      │
      ▼
persistir Message(role="user")
      │
      ▼
embed(pregunta) → retrieve(top-5, filtrado por room_id)
      │
      ▼
armar prompt (RAG_SYSTEM_PROMPT + contexto + pregunta)
      │
      ▼
stream token a token ──► SSE al cliente
      │
      ▼
persistir Message(role="assistant", citations=[...]) + commit
```

El prompt del sistema exige citar como `[Fuente N]`, no inventar información fuera del contexto, y responder en el idioma de la pregunta.

**Adapter pattern:** todo el acceso a proveedores de IA pasa por `LLMAdapter` / `GeminiAdapter` (`rag/llm_adapter.py`). El resto del código nunca habla directo con el SDK de Gemini, solo con la interfaz abstracta — lo que permitiría cambiar de proveedor sin tocar `service.py`.

## 8. Generación de material de estudio

Tres artefactos, todos generados pidiéndole a Gemini **JSON estricto** (`complete_json`), que luego se valida y persiste defensivamente — un LLM siempre puede desviarse del contrato.

### Resúmenes
Un documento → un resumen + puntos clave.

### Flashcards
Cada tarjeta debe traer obligatoriamente un `source_reference`: la cita textual exacta del material que respalda la respuesta, para trazabilidad. El campo `room_id` de la flashcard es denormalizado (se podría derivar vía `document.room_id`), pero está ahí por requerimiento de producto para consultas sin join — el invariante es que **siempre** se deriva del documento en `service.py`, nunca se confía en el caller ni en la salida del modelo.

### Exámenes
Dos rutas:
- **Síncrona, por documento** (la original): genera y persiste en la misma request.
- **Asíncrona, por sala completa**, vía Celery (`generate_exam_task`): agrega contexto de todos los documentos de la sala, sigue el mismo patrón `PENDING → READY/FAILED` que la ingesta de documentos, y el frontend hace polling de `GET /exams/{id}`.

Ciclo de vida de un intento:

```
start_attempt → submit_answer (por pregunta, sobrescribible) → submit_attempt
```

El índice de la respuesta correcta nunca se expone hasta `submit_attempt` — recién ahí se califica todo y se arma el breakdown.

### Repetición espaciada (SM-2)

`sm2.py` es una función pura sin ORM — recibe el estado actual (`ease`, `interval_days`, `repetitions`) y una calificación de 0 a 5, devuelve el nuevo estado y la próxima fecha de repaso:

- **`grade < 3`** → lapso: repeticiones a 0, intervalo a 1 día (se repasa mañana). El `ease` se penaliza igual.
- **`grade >= 3`** → éxito: repeticiones +1, el intervalo crece (1 día → 6 días → `intervalo_anterior × ease` en adelante).
- `ease` tiene piso en `1.3` para que una tarjeta crónicamente difícil no colapse a intervalo cero.

Este cálculo se aplica por usuario **y** por tarjeta (`FlashcardReview`), así que cada persona tiene su propio calendario de repaso sobre la misma tarjeta compartida.

## 9. Docker: cómo está armado y para qué sirve cada servicio

XookHub no corre como un único proceso — es un **stack de 7 contenedores** orquestados por `docker-compose.yml`, donde cada uno resuelve una responsabilidad puntual (base de datos, cola de mensajes, storage de archivos, proxy...). Nada de esto es opcional para desarrollo local: el `api` no arranca sano si Postgres, RabbitMQ o MinIO no están arriba y saludables primero.

### 9.1. Una sola imagen, dos roles

El `Dockerfile` construye **una única imagen** compartida por los servicios `api` y `worker` — no hay un Dockerfile por servicio. Tiene sentido: ambos ejecutan exactamente el mismo código Python (`src/`), la única diferencia es qué comando se lanza al arrancar el contenedor:

```dockerfile
FROM python:3.12-slim
# ... instala build-essential + curl, copia requirements.txt e instala deps
# ... copia src/, alembic.ini y alembic/
CMD ["uvicorn", "src.main:app", "--host", "0.0.0.0", "--port", "8000"]
```

`docker-compose.yml` **sobrescribe** ese `CMD` por servicio:

```yaml
api:
  command: uvicorn src.main:app --host 0.0.0.0 --port 8000 --reload
worker:
  command: celery -A src.worker.celery_app worker --loglevel=info --queues=xookhub.default,xookhub.documents
```

Un detalle deliberado de la imagen: `COPY requirements.txt .` y el `pip install` ocurren **antes** de `COPY src ./src`. Así, mientras solo cambies código de la app, Docker reutiliza la capa cacheada de dependencias y no las reinstala en cada rebuild — solo se invalida ese caché cuando cambia `requirements.txt`.

### 9.2. Los 7 servicios, uno por uno

```
                    ┌──────────┐
   navegador ──────►│  nginx   │  (puerto 80, único punto de entrada externo)
                    └────┬─────┘
                         │ proxy_pass
                    ┌────▼─────┐        ┌──────────┐
                    │   api    │◄──────►│    db    │  (Postgres 16 + pgvector)
                    │(FastAPI) │        └──────────┘
                    └────┬─────┘              ▲
                         │ .delay(task)        │
                    ┌────▼──────┐              │
                    │ rabbitmq  │              │
                    │ (broker)  │              │
                    └────┬──────┘              │
                         │ consume              │
                    ┌────▼─────┐                │
                    │  worker   │───────────────┘
                    │ (Celery)  │
                    └────┬──────┘
                         │ lee/escribe archivos
                    ┌────▼─────┐        ┌──────────┐
                    │  minio   │        │  redis   │  (cache + result backend)
                    └──────────┘        └──────────┘
```

| Servicio | Imagen | Para qué sirve exactamente |
|---|---|---|
| **`db`** | `pgvector/pgvector:pg16` | Postgres estándar + la extensión `pgvector`, que agrega el tipo de columna `vector(N)` y el operador de distancia coseno `<=>` usado en `DocumentChunk.embedding` para el retrieval de RAG (ver §7). Sin esta imagen específica, las migraciones que crean columnas `vector` fallarían. |
| **`redis`** | `redis:7-alpine` | Dos usos: *result backend* de Celery (`REDIS_URL` en `celery_app.py`, para poder consultar el estado/resultado de una tarea) y cache general de la app. No almacena nada del dominio (documentos, usuarios) — es puro estado efímero. |
| **`rabbitmq`** | `rabbitmq:3-management-alpine` | El **broker** de Celery: la cola de mensajes donde `api` deposita tareas (`process_document_task.delay(...)`, `generate_exam_task.delay(...)`) y de donde `worker` las consume. Expone además la consola de administración en el puerto `15672` para inspeccionar colas y mensajes en vuelo. |
| **`minio`** | `minio/minio` | Storage de objetos compatible con S3, donde viven los archivos subidos por los usuarios (los bytes reales de cada documento, bajo la ruta `rooms/{room_id}/{document_id}/{filename}` — ver §6). Postgres solo guarda la *referencia* (`Document.file_path`), nunca el archivo en sí. |
| **`minio-init`** | `minio/mc` | Un contenedor **one-shot** (no queda corriendo): espera a que `minio` esté sano, y ejecuta `scripts/init-minio.sh`, que crea el bucket (`MINIO_BUCKET`) si todavía no existe. Corre una sola vez por `docker compose up` y termina — por eso `api`/`worker` declaran `depends_on: minio-init: condition: service_completed_successfully` en vez de `service_healthy`. |
| **`api`** | build local (`Dockerfile`) | El proceso FastAPI (`uvicorn`) que atiende cada request HTTP: valida el JWT, ejecuta la lógica de cada `service.py`, lee/escribe en `db`, sube archivos a `minio`, y encola trabajo pesado en `rabbitmq` en vez de bloquear la respuesta. Corre con `--reload`, así que los cambios en `./src` (montado como volumen) se reflejan sin reconstruir la imagen. |
| **`worker`** | build local (misma imagen que `api`) | El proceso Celery que ejecuta en segundo plano lo que `api` encoló: ingesta de documentos (parseo, chunking, embeddings) y generación de exámenes de sala completa. Escucha dos colas: `xookhub.default` y `xookhub.documents` (ver §9.4). |
| **`nginx`** | `nginx:1.27-alpine` | El **único puerto expuesto al exterior** (`80:80`) — todo lo demás vive solo en la red interna de Docker Compose. Actúa como *API Gateway*: reenvía `/api/*` hacia `api:8000` y expone `/health` sin pasar por el prefijo `/api`. |

### 9.3. Por qué existe Nginx (no es un simple passthrough)

`nginx/nginx.conf` tiene una razón de ser muy concreta, no es boilerplate: **el streaming SSE de la conversación con RAG** (`POST /api/v1/conversations/{id}/messages`, ver §7‑B) se rompería si Nginx bufferea la respuesta antes de reenviarla al navegador — los tokens llegarían todos de golpe al final, en vez de uno a uno, matando el efecto de "escritura en vivo".

Por eso ese endpoint tiene su **propio `location`**, evaluado antes que el genérico `/api/`, con directivas específicas:

```nginx
location ~ ^/api/v1/conversations/[^/]+/messages$ {
    proxy_buffering       off;    # no acumules la respuesta, reenvía apenas llega
    chunked_transfer_encoding on;
    proxy_read_timeout    3600s;  # es un stream largo, no lo cortes por timeout
}
```

Nginx también fija `client_max_body_size 55m` — coincide (con margen) con el límite de 50 MB que `DocumentService` ya aplica a nivel de aplicación, así un upload grande no muere antes de tiempo con un genérico `413` de Nginx sin pasar por la validación real.

### 9.4. Cómo se comunican entre sí (redes, volúmenes, orden de arranque)

- **Red interna:** Docker Compose crea una red virtual donde cada servicio se resuelve por su *nombre* (`db`, `redis`, `rabbitmq`, `minio`, `api`) en vez de por IP — por eso `DB_URL` dentro del contenedor apunta a `db:5432` y no a `localhost`, aunque desde tu máquina host el mismo Postgres se vea en el puerto `5433` (remapeado para evitar choques con instalaciones locales en Windows).
- **Arranque ordenado, no solo secuencial:** cada servicio que depende de otro usa `depends_on: condition: service_healthy` (no un `depends_on` plano), así `api`/`worker` esperan a que `db`, `rabbitmq` y `minio-init` estén *realmente* listos para aceptar conexiones — no solo a que el proceso haya arrancado.
- **Volúmenes con nombre** (`pgdata`, `miniodata`) persisten los datos de Postgres y MinIO entre reinicios de contenedores — `docker compose down` los conserva; solo `make clean` (`down -v`) los destruye.
- **Bind mounts de código** (`./src:/app/src` en `api` y `worker`) permiten editar código en el host y verlo reflejado sin reconstruir la imagen — clave para el `--reload` de uvicorn.
- **Colas de Celery separadas** (`xookhub.default` y `xookhub.documents`, declaradas en `celery_app.conf.task_routes`): la ingesta de documentos tiene su propia cola dedicada, para que un pico de subidas no compita por el mismo carril con otras tareas futuras que se agreguen a `xookhub.default`.

**Detalle de robustez que vale la pena resaltar:** `worker/celery_app.py` importa explícitamente **todos** los módulos de modelos y llama `configure_mappers()` al arrancar. El worker es un proceso separado que nunca pasa por `main.py` — sin este import manual, la primera tarea real fallaría silenciosamente con reintentos cada 30 segundos (`max_retries` veces), en vez de fallar ruidosamente al arrancar el contenedor.

### 9.5. El día a día: `Makefile`

El `Makefile` envuelve los comandos de `docker compose` más usados, para no tener que recordar banderas:

| Comando | Qué hace |
|---|---|
| `make up` | Construye (si hace falta) y levanta todo el stack en segundo plano |
| `make down` | Detiene y elimina los contenedores — conserva los volúmenes (los datos sobreviven) |
| `make logs` / `make api-logs` / `make worker-logs` | Sigue los logs de todo el stack o de un servicio puntual |
| `make migrate` | Corre `alembic upgrade head` **dentro** del contenedor `api` — comparte su mismo entorno, red y dependencias, así no hace falta un Python local con `asyncpg` instalado |
| `make makemigration m="mensaje"` | Autogenera una migración de Alembic a partir de los cambios en los `models.py` |
| `make shell` | Abre una shell dentro del contenedor `api`, para debug manual |
| `make psql` | Abre una sesión `psql` contra el servicio `db` |
| `make create-bucket` | Vuelve a correr el bootstrap de MinIO manualmente (normalmente automático en `make up`) |
| `make clean` | `down -v` — **destruye también los volúmenes** (borra todos los datos); usar con cuidado |
