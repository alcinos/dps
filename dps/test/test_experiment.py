import pytest
from dps.experiments.simple_addition import train_addition


@pytest.mark.parametrize('config', ['default', 'rl'])
def test_simple_addition(config):
    train_addition(log_dir='/tmp/dps/addition', config=config)
