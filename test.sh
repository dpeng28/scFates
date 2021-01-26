docker run -it --rm -v /home/lfaure/scFates/:/rapids/rapids/ louisfaure/scfates:env-ready /bin/bash -c "pip install . coverage mock git+https://github.com/j-bac/elpigraph-python.git && coverage run -m pytest scFates/tests/ && coverage report -m"
