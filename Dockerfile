FROM python:3.13-slim

WORKDIR /app

ENV TGDL_DATA_DIR=./data
ENV TGDL_DISABLE_TQDM=true
ENV TZ=Asia/Shanghai

RUN apt-get update && apt-get install -y tzdata && \
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY main.py ./
COPY requirements.txt ./

RUN pip install -r requirements.txt

CMD ["python", "main.py"]
