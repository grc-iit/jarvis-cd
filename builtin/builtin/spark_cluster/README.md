# Installation

Manual build:
```
spack install openjdk@11
spack load openjdk@11
scspkg create spark
cd `scspkg pkg src spark`
wget https://dlcdn.apache.org/spark/spark-3.5.1/spark-3.5.1.tgz
tar -xzf spark-3.5.1.tgz
cd spark-3.5.1
./build/mvn -T 16 -DskipTests clean package
scspkg env set spark SPARK_SCRIPTS=${PWD}
scspkg env prepend spark PATH "${PWD}/bin"
module load spark
```
NOTE: this took 30min in Ares.

With spack (doesn't seem to work, sorry):
```
spack install spark
spack load spark
scspkg create spark-env
scspkg env set spark-env SPARK_SCRIPTS=`spack find --format "{PREFIX}" spark`
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
jarvis pipeline env build +SPARK_SCRIPTS
```

# Append the Spark Cluster Pkg

```
jarvis pipeline append spark_cluster
```

# Run the pipeline

```
jarvis pipeline run
```

