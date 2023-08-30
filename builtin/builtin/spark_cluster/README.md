# Installation

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

