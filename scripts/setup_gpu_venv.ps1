param(
    [string]$VenvPath = ".venv-gpu",
    [string]$TorchIndexUrl = "https://download.pytorch.org/whl/cu128",
    [switch]$SkipEngineInstall
)

$ErrorActionPreference = "Stop"
$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Invoke-PipStep {
    param([string[]]$Arguments)
    & $Python @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed: $Python $($Arguments -join ' ')"
    }
}

if (-not (Test-Path $VenvPath)) {
    & .\venv\Scripts\python.exe -m venv $VenvPath
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to create $VenvPath"
    }
}

$Python = Join-Path $VenvPath "Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "GPU venv python not found at $Python"
}

Invoke-PipStep @("-m", "pip", "install", "--upgrade", "pip", "setuptools", "wheel")

if (-not $SkipEngineInstall) {
    Invoke-PipStep @("-m", "pip", "install", "-r", "requirements.txt")
    # marker-pdf and MinerU currently declare incompatible Pillow ranges.
    # Keep Marker/Surya dependencies from requirements, then add MinerU and its
    # runtime deps with explicit versions for benchmark use in this isolated env.
    Invoke-PipStep @("-m", "pip", "install", "mineru==3.1.15", "--no-deps")
    Invoke-PipStep @(
        "-m", "pip", "install",
        "loguru",
        "fast-langdetect>=0.2.3,<0.3.0",
        "json-repair",
        "magika",
        "mammoth",
        "mineru-vl-utils>=0.2.7,<1",
        "modelscope",
        "pandas>=2.3.3,<3",
        "opencv-python",
        "pypptx-with-oxml",
        "qwen-vl-utils",
        "reportlab",
        "scikit-image",
        "boto3"
        "albumentations"
    )
}

try {
    Invoke-PipStep @("-m", "pip", "install", "--upgrade", "--force-reinstall", "torch", "torchvision", "--index-url", $TorchIndexUrl)
}
catch {
    Write-Warning "PyTorch install from $TorchIndexUrl failed. Retrying with CUDA 12.6 wheels."
    Invoke-PipStep @("-m", "pip", "install", "--upgrade", "--force-reinstall", "torch", "torchvision", "--index-url", "https://download.pytorch.org/whl/cu126")
}

Invoke-PipStep @(
    "-c",
    "import os, sys; os.environ['TORCH_DEVICE']='cuda'; os.environ['MINERU_DEVICE_MODE']='cuda'; import torch; print('python=', sys.executable); print('torch=', torch.__version__); print('torch_cuda_runtime=', torch.version.cuda); print('cuda_available=', torch.cuda.is_available()); print('device_count=', torch.cuda.device_count()); print('device_name=', torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'none'); from surya.settings import settings; print('surya_device=', settings.TORCH_DEVICE_MODEL); from mineru.utils.config_reader import get_device; print('mineru_device=', get_device())"
)
