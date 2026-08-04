#!/usr/bin/env bash
# Выкатка на хосте: пересобрать образ из лежащих рядом исходников и перезапустить.
# Вызывается из CI по SSH, но аргументов нет — руками запускается точно так же.
set -euo pipefail

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    echo "❌ рядом с compose.yaml нет .env — секреты живут на хосте, в гите их нет"
    exit 1
fi

docker compose up -d --build --remove-orphans

# Дым: мало того что контейнер жив — он должен успеть доложить, что вошёл
# в Plane и видит чаты. Упавший логин иначе выглядел бы как успешная выкатка.
for _ in $(seq 1 30); do
    if docker compose logs --since 3m bugbot 2>/dev/null | grep -q "слушаем"; then
        echo "✅ бот поднялся и слушает чаты"
        docker compose ps
        exit 0
    fi
    if [ "$(docker compose ps -q bugbot | xargs -r docker inspect -f '{{.State.Running}}')" != "true" ]; then
        echo "❌ контейнер упал"
        docker compose logs --tail 50 bugbot
        exit 1
    fi
    sleep 2
done

echo "❌ за 60 секунд бот не доложил, что слушает чаты"
docker compose logs --tail 50 bugbot
exit 1
