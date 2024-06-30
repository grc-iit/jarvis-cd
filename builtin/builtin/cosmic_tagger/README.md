# Conda
Get the miniconda3 installation script and run it
```
wget https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash Miniconda3-latest-Linux-x86_64.sh
```

# Cosmic Tagger
[Cosmic Tagger](https://github.com/coreyjadams/CosmicTagger) trains a CNN to separate cosmic pixels.

Conda:
```
conda create -n cosmic_tagger python==3.7
conda activate cosmic_tagger
conda install cmake hdf5 scikit-build numpy
```

Install Larc3
```
git clone https://github.com/DeepLearnPhysics/larcv3.git
cd larcv3
git submodule update --init
pip install -e .
```

Download cosmic tagger
```
git clone https://github.com/coreyjadams/CosmicTagger.git
cd CosmicTagger
pip install -r requirements.txt
```