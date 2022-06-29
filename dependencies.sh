#!/bin/bash

#zlib, zlib-devel, make, cmake, openssl-devel

####VARIABLES
#PREFIX: the place where to install dependencies
if [[ -z "${PREFIX}" ]]; then
  PREFIX=${HOME}
fi

#Create the directory used to house all dependencies
mkdir -p $PREFIX

#Detect python version
PYTHON_NEEDED=1
if command -v python3
then
  PYTHON_VERSION=`python3 --version`
  [[ $PYTHON_VERSION =~ ([0-9]+)\.([0-9]+)\.([0-9]+) ]]
  PYTHON_MAJOR=${BASH_REMATCH[1]}
  PYTHON_MINOR=${BASH_REMATCH[2]}
fi
if [[ $PYTHON_MAJOR -ge 3 && $PYTHON_MINOR -ge 6 ]]
then
  PYTHON_NEEDED=0
fi

#Source bashni in bashrc at head of file
if [[ `cat ~/.bashrc | grep "source ~/.bashni"` ]]
then
  echo "Already sourcing ~/.bashni"
else
  echo "Adding source ~/.bashni to bashrc"
  sed -i.old "1s;^;source ~/.bashni\\n;" ~/.bashrc
fi

#Install python if necessary
if [[ $PYTHON_NEEDED -eq 1 ]]
then
  PYTHON_DIR=${PREFIX}/python3.6
  echo "Installing Python 3.6"
  mkdir -p ${PYTHON_DIR}/src
  cd ${PYTHON_DIR}/src
  wget https://www.python.org/ftp/python/3.6.14/Python-3.6.14.tgz
  tar -xzf Python-3.6.14.tgz
  cd Python-3.6.14
  ./configure --prefix=${PYTHON_DIR}
  make -j8
  make install

  PATH=${PYTHON_DIR}/bin:${PATH}
  LD_LIBRARY_PATH=${PYTHON_DIR}/lib:${PATH}
  LIBRARY_PATH=${PYTHON_DIR}/lib:${PATH}
  CPATH=${PYTHON_DIR}/include:${PATH}

  echo "export PATH=$PATH" >> ~/.bashni
  echo "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH" >> ~/.bashni
  echo "export LIBRARY_PATH=$LIBRARY_PATH" >> ~/.bashni
  echo "export CPATH=$CPATH" >> ~/.bashni
  source ~/.bashni

  pip3 install --upgrade pip
fi