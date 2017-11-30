#!/usr/bash
set -ev
pip install -r requirements_dev.txt
MNIST_ARITHMETIC_PATH=$(python -c "import mnist_arithmetic; from pathlib import Path; print(Path(mnist_arithmetic.__file__).parent.parent)")
DATA_DIR="$TRAVIS_BUILD_DIR"/data
mkdir "$DATA_DIR"

echo "\nDownloading and processing emnist data..."
time python "$MNIST_ARITHMETIC_PATH"/download.py emnist "$DATA_DIR"
rm matlab.zip

echo "\nDownloading and processing omniglot data..."
time python "$MNIST_ARITHMETIC_PATH"/download.py omniglot "$DATA_DIR"