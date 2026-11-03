param(
  [ValidateSet('compile','upload','ports')][string]$Action='compile',
  [string]$SketchPath,
  [string]$Port,
  [string]$Cli='arduino-cli'
)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path $PSScriptRoot -Parent
if (!$SketchPath) { $SketchPath=Join-Path $taskRoot 'arduino/W09_CloudCamera' }
$buildPath=Join-Path $taskRoot '.local/w09-build'
$fqbn='esp32:esp32:esp32s3:FlashSize=16M,PSRAM=opi,USBMode=hwcdc,CDCOnBoot=cdc,PartitionScheme=app3M_fat9M_16MB'
if ($Action -eq 'ports') { & $Cli board list; exit $LASTEXITCODE }
if (!(Test-Path -LiteralPath $SketchPath -PathType Container)) { throw '先完成小組W9 sketch，或用 -SketchPath 指向自己的程式資料夾。' }
if ($Action -eq 'compile') {
  & $Cli compile --fqbn $fqbn --build-path $buildPath $SketchPath
} else {
  if (!$Port) { throw '請用 -Port 指定目前ESP32的USB埠，先用 -Action ports 查看。' }
  if (!(Test-Path -LiteralPath $buildPath -PathType Container)) { throw '請先編譯本組W9程式。' }
  & $Cli upload --fqbn $fqbn --port $Port --input-dir $buildPath $SketchPath
}
exit $LASTEXITCODE
