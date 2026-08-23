param(
    [string]$InputPptx = "astra-mechanical-engineering-portfolio.pptx",
    [string]$OutputPdf = "astra-mechanical-engineering-portfolio.pdf"
)

$ErrorActionPreference = "Stop"
$scriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path

function Resolve-PortfolioPath {
    param([string]$PathValue)
    if ([System.IO.Path]::IsPathRooted($PathValue)) {
        return [System.IO.Path]::GetFullPath($PathValue)
    }
    return [System.IO.Path]::GetFullPath((Join-Path $scriptDirectory $PathValue))
}

$inputPath = Resolve-PortfolioPath $InputPptx
$outputPath = Resolve-PortfolioPath $OutputPdf

if (-not (Test-Path -LiteralPath $inputPath -PathType Leaf)) {
    throw "PowerPoint file not found: $inputPath"
}

$outputDirectory = Split-Path -Parent $outputPath
if (-not (Test-Path -LiteralPath $outputDirectory -PathType Container)) {
    New-Item -ItemType Directory -Path $outputDirectory | Out-Null
}

$powerPoint = $null
$presentation = $null
try {
    $powerPoint = New-Object -ComObject PowerPoint.Application
    $presentation = $powerPoint.Presentations.Open($inputPath, $true, $false, $false)
    $presentation.SaveAs($outputPath, 32)
}
finally {
    if ($null -ne $presentation) {
        $presentation.Close()
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($presentation)
    }
    if ($null -ne $powerPoint) {
        $powerPoint.Quit()
        [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($powerPoint)
    }
    [GC]::Collect()
    [GC]::WaitForPendingFinalizers()
}

if (-not (Test-Path -LiteralPath $outputPath -PathType Leaf)) {
    throw "PDF export did not create the expected file: $outputPath"
}

Write-Output $outputPath
