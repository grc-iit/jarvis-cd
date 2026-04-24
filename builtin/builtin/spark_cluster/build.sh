#!/bin/bash
set -e

export DEBIAN_FRONTEND=noninteractive

SPARK_VERSION=3.5.1
HADOOP_VERSION=3
SPARK_DIR=spark-${SPARK_VERSION}-bin-hadoop${HADOOP_VERSION}
SPARK_TGZ=${SPARK_DIR}.tgz

apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# Prebuilt Spark tarball from the Apache archive — no source build.
curl -fsSL "https://archive.apache.org/dist/spark/spark-${SPARK_VERSION}/${SPARK_TGZ}" \
        -o "/tmp/${SPARK_TGZ}"
tar -xzf "/tmp/${SPARK_TGZ}" -C /opt
ln -sfn "/opt/${SPARK_DIR}" /opt/spark
rm "/tmp/${SPARK_TGZ}"
