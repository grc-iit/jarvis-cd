#!/bin/bash

####VARIABLES
#PREFIX: the place where to install dependencies
if [[ -z "${PREFIX}" ]]; then
  PREFIX=`pwd`
fi

#Create the directory used to house all dependencies
mkdir -p $PREFIX

#Determine whether or not to install python3.6
if [[ $DO_PYTHON ]]; then
  echo "DO_PYTHON=$DO_PYTHON"
else
  DO_PYTHON=1
  if command -v python3
  then
    PYTHON_VERSION=`python3 --version`
    [[ $PYTHON_VERSION =~ ([0-9]+)\.([0-9]+)\.([0-9]+) ]]
    PYTHON_MAJOR=${BASH_REMATCH[1]}
    PYTHON_MINOR=${BASH_REMATCH[2]}
  fi
  if [[ $PYTHON_MAJOR -ge 3 && $PYTHON_MINOR -ge 6 ]]
  then
    DO_PYTHON=0
  fi
fi

#Source jarvis_env in bashrc at head of file
if [[ `cat ${HOME}/.bashrc | grep "source ${PWD}/.jarvis_env"` ]]
then
  echo "Already sourcing ${PWD}/.jarvis_env"
else
  echo "Adding source ${PWD}/.jarvis_env to bashrc"
  sed -i.old "1s;^;source ${PWD}/.jarvis_env\\n;" ${HOME}/.bashrc
fi
touch ${PWD}/.jarvis_env

#chmod +x all jarvis binaries
chmod +x bin/*

#Create jarvis repos directory
mkdir jarvis_repos
rm `pwd`/jarvis_repos/builtin
ln -s `pwd`/builtin `pwd`/jarvis_repos/builtin

#Create repo env directory
mkdir -p ${PWD}/jarvis_envs/${USER}

#Install python if necessary
if [[ $DO_PYTHON -eq 1 ]]
then
  exit
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

  echo "export PATH=$PATH" >> ${PWD}/.jarvis_env
  echo "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH" >> ${PWD}/.jarvis_env
  echo "export LIBRARY_PATH=$LIBRARY_PATH" >> ${PWD}/.jarvis_env
  echo "export CPATH=$CPATH" >> ${PWD}/.jarvis_env
  source ${HOME}/.jarvis_env

  pip3 install --upgrade pip
fi