# Foam-Agent evaluation commands

Commands were run on 2026-06-21. The repository itself was not modified.

## Runtime setup

```powershell
wsl --manage Ubuntu-22.04 --move E:\WSL\Ubuntu-22.04
```

```bash
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get -y install docker.io
systemctl enable --now docker
docker pull leoyue123/foamagent:latest

wget -qO /etc/apt/trusted.gpg.d/openfoam.asc https://dl.openfoam.org/gpg.key
add-apt-repository -y http://dl.openfoam.org/ubuntu
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get -y install openfoam10

wget -O /tmp/miniconda.sh https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh
bash /tmp/miniconda.sh -b -p /opt/conda
/opt/conda/bin/conda env create --file /opt/Foam-Agent/environment.yml --yes --quiet
```

The Docker pull was attempted twice and failed while extracting its largest layer. The successful evaluation runtime therefore used the repository Conda environment plus the official Foundation OpenFOAM v10 Ubuntu package.

## Release and environment evidence

```powershell
git -C 'E:\cursor projects\Astra\Foam-Agent' rev-parse HEAD
git -C 'E:\cursor projects\Astra\Foam-Agent' describe --tags --exact-match
git ls-remote https://github.com/csml-rpi/Foam-Agent.git refs/heads/main refs/tags/v2.1.0
```

```bash
source /opt/openfoam10/etc/bashrc
foamVersion
dpkg-query -W openfoam10
/opt/conda/envs/FoamAgent/bin/python --version
/opt/conda/bin/conda --version
```

## Checked-in tests

All Codex commands used `FOAMAGENT_MODEL_PROVIDER=openai-codex` and the supported subscription model `gpt-5.5`. `run_with_codex_auth.py` creates a mode-0600 compatibility projection in WSL `/run` and removes it when the command exits; it never prints token values.

```bash
source /opt/openfoam10/etc/bashrc
export FOAMAGENT_MODEL_PROVIDER=openai-codex
export FOAMAGENT_MODEL_VERSION=gpt-5.5
export PYVISTA_OFF_SCREEN=true
cd /opt/Foam-Agent

/usr/bin/python3 /opt/Foam-Eval/scripts/run_with_codex_auth.py \
  /opt/conda/envs/FoamAgent/bin/python -m pytest tests -v

/usr/bin/python3 /opt/Foam-Eval/scripts/run_with_codex_auth.py \
  /opt/conda/envs/FoamAgent/bin/python tests/test_lid_driven_cavity_services.py

/usr/bin/python3 /opt/Foam-Eval/scripts/run_with_codex_auth.py \
  /opt/conda/envs/FoamAgent/bin/python -m src.mcp.fastmcp_server \
  --transport http --host 0.0.0.0 --port 8080

/opt/conda/envs/FoamAgent/bin/python tests/test_lid_driven_cavity_mcp.py
```

## Requested end-to-end runs

The output was placed on the WSL ext4 filesystem because the MCP run proved that the runner does not quote Windows-mounted paths containing spaces.

```bash
source /opt/openfoam10/etc/bashrc
export PATH=/opt/conda/envs/FoamAgent/bin:/opt/openfoam10/platforms/linux64GccDPInt32Opt/bin:/opt/openfoam10/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export FOAMAGENT_MODEL_PROVIDER=openai-codex
export FOAMAGENT_MODEL_VERSION=gpt-5.5
export PYVISTA_OFF_SCREEN=true
cd /opt/Foam-Agent

/usr/bin/python3 /opt/Foam-Eval/scripts/run_with_codex_auth.py \
  /opt/conda/envs/FoamAgent/bin/python foambench_main.py \
  --output /opt/foamagent-eval/run1 \
  --prompt_path /opt/Foam-Eval/prompts/cavity_re100.txt

/usr/bin/python3 /opt/Foam-Eval/scripts/run_with_codex_auth.py \
  /opt/conda/envs/FoamAgent/bin/python foambench_main.py \
  --output /opt/foamagent-eval/run2 \
  --prompt_path /opt/Foam-Eval/prompts/cavity_re100.txt
```

## Independent checks

```bash
cd /opt/foamagent-eval/run1
source /opt/openfoam10/etc/bashrc
set -o pipefail
checkMesh -case . -allGeometry -allTopology 2>&1 | tee independent_checkMesh.log
postProcess -case . -func patchFlowRate\(patch=movingWall\) -latestTime
postProcess -case . -func patchFlowRate\(funcName=fixedWallsFlow,patch=fixedWalls\) -latestTime

/opt/conda/envs/FoamAgent/bin/python /opt/Foam-Eval/scripts/validate_cavity.py \
  /opt/foamagent-eval/run1 /opt/Foam-Eval/artifacts/run1

/opt/conda/envs/FoamAgent/bin/python /opt/Foam-Eval/scripts/validate_cavity.py \
  /opt/foamagent-eval/run2 /opt/Foam-Eval/artifacts/run2

/opt/conda/envs/FoamAgent/bin/python /opt/Foam-Eval/scripts/compare_cavity_runs.py \
  /opt/foamagent-eval/run1 /opt/foamagent-eval/run2 \
  /opt/Foam-Eval/artifacts/reproducibility.json
```
