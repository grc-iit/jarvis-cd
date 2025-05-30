# paraview 


ParaView is an open-source data analysis and visualization application designed to handle large-scale scientific datasets. It supports interactive and batch processing for visualizing complex simulations and performing quantitative analysis.

# Installation

```bash
spack install paraview
```

# how to use

In ares, run the following command
```bash
spack load paraview
jarvis ppl create paraview
jarvis ppl env build
jarvis ppl append paraview port_id=11111
jarvis ppl run
```

Run this command in local terminal:
```
ssh -N -L 11111:localhost:11111 your_id@ares.cs.iit.edu
```
In local paraview, following these instructions:
File -> connect </br>
Then set the port number and connect to Ares.  
