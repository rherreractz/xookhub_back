set -eu

: "${MINIO_ROOT_USER:?MINIO_ROOT_USER no está definido}"
: "${MINIO_ROOT_PASSWORD:?MINIO_ROOT_PASSWORD no está definido}"
: "${MINIO_BUCKET:?MINIO_BUCKET no está definido}"

ALIAS="myminio"
ENDPOINT="http://minio:9000"

mc alias set "$ALIAS" "$ENDPOINT" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD"

echo "Esperando a que MinIO esté listo en $ENDPOINT..."
until mc ready "$ALIAS" >/dev/null 2>&1; do
  echo "  MinIO aún no responde, reintentando en 2s..."
  sleep 2
done
echo "MinIO listo."

mc mb --ignore-existing "$ALIAS/$MINIO_BUCKET"
echo "Bucket '$MINIO_BUCKET' verificado/creado."