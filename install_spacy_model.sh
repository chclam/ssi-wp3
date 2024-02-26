#!/bin/sh
# When installing a spacy model, pip wants to install spacy as well, which will be blocked by our proxy
# Therefore install without dependencies 
pip install --no-deps $1
