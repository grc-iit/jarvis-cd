import setuptools
import os
import sys 
import shutil

cur_dir = os.path.dirname(os.path.realpath(__file__))

setuptools.setup(
    name="jarvis_cd",
    packages=setuptools.find_packages(),
    scripts=['bin/jarvis.py'],
    # scripts=[f'bin/{f}' for f in os.listdir(os.path.join(cur_dir, 'bin'))],
    version="0.0.1",
    author="Luke Logan",
    author_email="llogan@hawk.iit.edu",
    description="Create basic for applications",
    long_description=open('README.md').read(),
    long_description_content_type='text/markdown',
    url="https://github.com/scs-lab/jarvis-cd",
    classifiers = [
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Development Status :: 0 - Pre-Alpha",
        "Environment :: Other Environment",
        "Intended Audience :: Developers",
        "License :: None",
        "Operating System :: OS Independent",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Application Configuration",
    ]
)

# Detect the current shell
supported_shells = {'csh', 'dash', 'elvish', 'fish', 'ksh', 'bash', 'tcsh', 'zsh'}
detected_shell = next((sh for sh in supported_shells if sh in shell_name), 'sh') 
print(f"Detected Shell: {detected_shell}") 

# Get environment variables and paths
if os.name == 'nt':
    install_dir = os.path.join(sys.prefix, 'Scripts')
else:
    install_dir = os.path.join(sys.prefix, 'bin') 
terp_path = sys.executable
python_env = os.getenv('PYTHONPATH', '')
jarvis_py_path = os.path.join(install_dir, 'jarvis.py')
jarvis_path = os.path.join(install_dir, 'jarvis')
path = os.getenv('PATH', '')
shell_path = os.environ.get('SHELL', '')
shell_name = os.path.basename(shell_path).lower()
jarvis_tmpl = os.path.join(cur_dir, 'bin', f'jarvis.{detected_shell}.tmpl')

# Create the jarvis script for the current shell
with open(os.path.join(cur_dir,jarvis_tmpl), 'r') as template:
    jarvis_template = template.read()
print(f'Creating {jarvis_path}')
with open(jarvis_path, 'w') as fp:
    fp.write(jarvis_template
            .replace('@PATH@', path)
            .replace('@PYTHON@', terp_path)
            .replace('@PYTHONPATH@', python_env)
            .replace('@JARVIS_PY_PATH@', jarvis_py_path))
os.chmod(jarvis_path, 0o755)

# Copy the jarvis script to ~/.jarvis
install_jarvis_path = os.path.join(os.environ['HOME'], '.jarvis', 'jarvis')
if not os.path.exists(os.path.dirname(install_jarvis_path)):
    os.makedirs(os.path.dirname(install_jarvis_path))
shutil.copy(jarvis_path, install_jarvis_path)

# Install the builtin directory to ~/.jarvis
local_builtin_path = os.path.join(cur_dir, 'builtin')
install_builtin_path = os.path.join(os.environ['HOME'], '.jarvis', 'builtin')  
if not os.path.exists(os.path.dirname(install_builtin_path)):
    os.makedirs(os.path.dirname(install_builtin_path))
shutil.copytree(local_builtin_path, install_builtin_path, dirs_exist_ok=True)
