docker run -it --rm -v /home/lfaure/scFates/:/rapids/rapids/ louisfaure/scfates:env-ready /bin/bash -c "pip install . pytest coverage mock git+https://github.com/j-bac/elpigraph-python.git && coverage run -m pytest scFates/tests/test_w_plots.py && coverage report -m"
