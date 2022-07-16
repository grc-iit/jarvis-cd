import setuptools

setuptools.setup(
    name="jarvis_cd",
    packages=setuptools.find_packages(),
    scripts=[
        'bin/jarvis-bootstrap', 'bin/jarvis', 'bin/jarvis-pssh',
        'bin/jarvis-kill', 'bin/jarvis-py-paths', 'bin/jarvis-ssh',
        'bin/jarvis-scp', 'bin/jarvis-mkdir', 'bin/jarvis-rm', 'bin/jarvis-exec'],
    version="0.0.1",
    author="Luke Logan",
    author_email="llogan@hawk.iit.edu",
    description="Create launcher for applications",
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
    ],
    long_description=""
)
