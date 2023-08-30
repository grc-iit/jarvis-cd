# Installation

Manual build:
```
spack install
scspkg create spark
wget https://dlcdn.apache.org/spark/spark-3.4.1/spark-3.4.1.tgz
tar -xzf spark-3.4.1.tgz
cd spark-3.4.1
./build/mvn -DskipTests clean package
scspkg set-env spark SPARK_SCRIPTS ${PWD}
module load spark
```

With spack (doesn't seem to work, sorry):
```
spack install spark
spack load spark
scspkg create spark-env
scspkg set-env spark-env SPARK_SCRIPTS `spack find --format "{PREFIX}" spark`
module load spark-env
```

Additional configuration documentation 
[here](https://spark.apache.org/docs/latest/spark-standalone.html).

# Create the Jarvis pipeline

```
jarvis pipeline create spark
```

# Build the Jarvis environment

```
jarvis pipeline env build SPARK_SCRIPTS=${SPARK_SCRIPTS}
```

# Append the Spark Cluster Pkg

```
jarvis pipeline append spark_cluster
```

# Run the pipeline

```
jarvis pipeline run
```

