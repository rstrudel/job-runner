import yaml
import datetime
import os
import getpass
import sys
import time
from collections import OrderedDict
import subprocess
import numpy as np
from pathlib import Path
import argparse

ROOT_DIR = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT_DIR / '.cache'
CACHE_DIR.mkdir(exist_ok=True)
CACHE_YAML_PATH = CACHE_DIR / 'cache.yaml'
N_COLS = 80


def file_iterator(f, delay=0.1):
    f = Path(f)
    if not f.exists():
        f.write_text('')
    f = f.open('r', newline='', buffering=1)
    while True:
        line = f.readline()
        if not line:
            time.sleep(delay)    # Sleep briefly
            yield None
        yield line


def make_job_script(flags, env, commands):
    script = '#!/bin/bash\n'
    for k, v in flags.items():
        script += f'#SBATCH --{k}={v}\n'
    script += '\n'

    for k, v in env.items():
        script += f'export {k}={v}\n'
    script += '\n'
    script += commands
    return script


def resolve_path(s):
    return Path(os.path.expandvars(s)).resolve()


def set_config():
    parser = argparse.ArgumentParser()
    parser.add_argument('config_path', metavar='config_path')
    args = parser.parse_args()
    config_path = resolve_path(args.config_path)
    CACHE_YAML_PATH.write_text(yaml.dump(dict(config_path=config_path)))
    print("Configuration file set to:", config_path)


def runjob():
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default='', type=str)
    parser.add_argument("--queue", default='', type=str)
    parser.add_argument("--ngpus", default=1, type=int)
    parser.add_argument("--time", default='', type=str)
    parser.add_argument("--jobid", default=str(np.random.randint(1e9)), type=str)
    parser.add_argument("--no-assign-gpu", dest='assign_gpu', action='store_false')
    parser.add_argument("command", nargs=argparse.REMAINDER, help='Command to be executed in each process')
    args = parser.parse_args()

    try:
        cache = yaml.load(CACHE_YAML_PATH.read_text(), Loader=yaml.FullLoader)
    except FileNotFoundError:
        raise ValueError('Please set your configuration file with runjob-config.')

    if not args.command:
        raise ValueError('Please provide a command to run in your job.')
    args.command = ' '.join(args.command)

    config_path = cache['config_path']
    print("Using config: ", config_path)
    cfg = yaml.load(config_path.read_text(), Loader=yaml.FullLoader)

    projects = cfg['projects']
    if not args.project:
        args.project = cfg['default_project']
    project = projects[args.project]

    queues = cfg['gpu_queues']
    if not args.queue:
        if 'default_queue' in project:
            args.queue = project['default_queue']
        else:
            args.queue = cfg['default_queue']
    queue = queues[args.queue]

    job_name = args.jobid
    storage_dir = resolve_path(cfg['storage']['root'])
    job_dir = storage_dir / job_name
    job_dir.mkdir(exist_ok=True)

    flags = queue['flags']
    if args.time:
        flags['time'] = args.time
    flags['ntasks'] = args.ngpus
    n_proc_per_node = min(args.ngpus, queue['n_gpus_per_node'])
    n_cpus_per_gpu = int(queue['n_cpus_per_node'] / queue['n_gpus_per_node'])
    flags['ntasks-per-node'] = n_proc_per_node
    flags['cpus-per-task'] = n_cpus_per_gpu
    flags['job-name'] = job_name
    flags['gres'] = f'gpu:{n_proc_per_node}'
    flags['output'] = job_dir / 'stdout.out'
    flags['error'] = job_dir / 'stdout.out'

    env = OrderedDict()
    env.update(
        JOB_DIR=job_dir,
        PROJECT_DIR=project['dir'],
        CONDA_ROOT=cfg['conda']['root'],
        CONDA_ENV=project['conda_env'],
        N_CPUS=flags['cpus-per-task'],
        N_PROCS=args.ngpus,
        PROC_ID='${SLURM_PROCID}',
        OUT_FILE='${JOB_DIR}/proc=${PROC_ID}.out'
    )

    bash_script = (config_path.parent / project['preamble']).read_text()
    conda_python = resolve_path(cfg['conda']['root']) / 'envs' / project['conda_env'] / 'bin/python'
    assign_gpu_path = Path(__file__).resolve().parent / 'assign_gpu.py'
    if args.assign_gpu:
        bash_script += '\n# This is automatically added by job-runner'
        bash_script += f'\neval $({conda_python} {assign_gpu_path})\n'
    bash_script += '\n'
    bash_script += args.command + ' &> $OUT_FILE'

    bash_script_path = job_dir / 'script.sh'
    bash_script_path.write_text(bash_script)

    script_command = f'srun bash {bash_script_path}'
    slurm_script_path = job_dir / 'script.slurm'
    slurm_script = make_job_script(flags, env, script_command)
    slurm_script_path.write_text(slurm_script)

    print(f"""
JOB DIRECTORY: {job_dir}
Content of script.slurm:
{'-'*N_COLS}
{slurm_script}
{'-'*N_COLS}

Content of script.sh:
{'-'*N_COLS}
{bash_script}
{'-'*N_COLS}""")

    def print_output():
        proc_output = subprocess.run(['sbatch', slurm_script_path])
        follow_file = job_dir / 'proc=0.out'
        start = datetime.datetime.now()
        print(f"Job submitted: {start}")
        print(f"Job output {follow_file}\n{'-'*N_COLS}")
        username = getpass.getuser()
        for text in file_iterator(follow_file):
            if text is not None:
                print(text, end="")
            jobinfo = subprocess.check_output(['squeue', '-u', username, '--name', job_name, '--noheader'])
            if len(jobinfo) == 0:
                break
        end = datetime.datetime.now()
        print(f"{'-'*N_COLS}")
        print(f"Job finished: {start} ({end - start})")

    try:
        print_output()
    except KeyboardInterrupt:
        subprocess.run(['scancel', '--name', job_name])
        print("\nJob cancelled.")


if __name__ == '__main__':
    runjob()