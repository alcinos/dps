export LD_LIBRARY_PATH=/cvmfs/soft.computecanada.ca/nix/var/nix/profiles/gcc-5.4.0/lib64:$LD_LIBRARY_PATH

python exploration.py --name=exploration_short --n-repeats=1 --max-hosts=1 --ppn=4 --cpp=1 --kind=slurm --wall-time=20mins --cleanup-time=2mins --slack-time=2mins --pmem=7000 --gpu-set=0 --ignore-gpu=False --error-on-timeout=False --n-param-settings=4

# python exploration.py --name=exploration --n-repeats=1 --max-hosts=1 --ppn=16 --cpp=1 --kind=slurm --wall-time=6hours --cleanup-time=30mins --slack-time=30mins --pmem=5000 --gpu-set=0,1,2,3 --ignore-gpu=False --error-on-timeout=False --n-param-settings=16