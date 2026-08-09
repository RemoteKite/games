# YSF XSEED 语音版中文补丁构建工具

本工具从用户自行准备的游戏文件和[原汉化补丁](https://keylol.com/t107903-1-1)在本地生成《伊苏：菲尔盖纳之誓》XSEED 语音版中文补丁。

仓库只需要保存本目录中的构建代码和说明，不需要也不应包含游戏文件、原汉化补丁文件或预先生成的补丁成品。

## 运行环境

- Windows 10 或 Windows 11；
- Python 3.10 或更高版本；
- 以下四个文件必须放在同一个目录：

```text
builder/
├─ build_patch.ps1
├─ build_patch.py
├─ build_manifest.json
└─ nani_parser.py
```

构建过程只使用 Python 标准库，无需安装额外 Python 软件包。

## 需要自行准备的输入

工具不会下载任何游戏或汉化资源。你必须自行指定下面三个目录，目录名称和存放位置可以任意设置，不必与 `builder` 放在一起。

### 1. 2017 英文版游戏根目录（Steam Build 2249175）

目录中至少应有：

```text
ysf_win_dx9.exe
config_dx9.exe
release/data_us.ni
release/data_us.na
```

### 2. XSEED 语音版游戏根目录（Steam Build 4766666）

目录中同样应有：

```text
ysf_win_dx9.exe
config_dx9.exe
release/data_us.ni
release/data_us.na
```

### 3. 原汉化补丁目录

请先把原汉化补丁解压到单独目录。该目录中至少应有：

```text
ysf_win_cn_dx9.exe
config_cn_dx9.exe
ysfcn.dll
ysfcn.text
font.ttf
release/data_cn.ni
release/data_cn.na
```

构建器会核验这些输入的 SHA-256。版本或内容不符合已验证素材时会停止，不会把固定地址和脚本映射应用到未知版本。

## 最简单的使用方法：按提示输入路径

在 `builder` 目录中打开 PowerShell，然后运行：

```powershell
.\build_patch.ps1
```

脚本会依次询问：

1. 2017 英文版游戏根目录；
2. XSEED 语音版游戏根目录；
3. 原汉化补丁目录；
4. 输出目录。

可以把资源管理器中的目录直接拖到 PowerShell 窗口。路径可以包含空格；脚本会去掉拖放时可能附带的引号。

输出目录必须尚不存在，这是为了避免误覆盖游戏或旧成品。例如可以填写：

```text
D:\YSF-Build\YSF_Chinese_Patch
```

如果 PowerShell 阻止运行本地脚本，可以只为当前窗口临时允许脚本：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\build_patch.ps1
```

## 非交互用法

也可以一次提供所有路径。下面的路径只是示例，不是固定要求：

```powershell
.\build_patch.ps1 `
  -Game2017 'D:\Games\YSF-2017' `
  -Game2020 'D:\Games\YSF-XSEED-Voice' `
  -OldPatch 'D:\Patches\YSF-Original-CN' `
  -Output 'D:\YSF-Build\YSF_Chinese_Patch'
```

如果 Python 没有加入 PATH，可以额外指定解释器：

```powershell
.\build_patch.ps1 `
  -Python 'C:\Python312\python.exe' `
  -Game2017 'D:\Games\YSF-2017' `
  -Game2020 'D:\Games\YSF-XSEED-Voice' `
  -OldPatch 'D:\Patches\YSF-Original-CN' `
  -Output 'D:\YSF-Build\YSF_Chinese_Patch'
```

## 直接运行 Python

不使用 PowerShell 包装脚本时，四个路径参数都是必填项：

```powershell
python .\build_patch.py `
  --game-2017 'D:\Games\YSF-2017' `
  --game-2020 'D:\Games\YSF-XSEED-Voice' `
  --old-patch 'D:\Patches\YSF-Original-CN' `
  --output 'D:\YSF-Build\YSF_Chinese_Patch'
```

运行 `python .\build_patch.py --help` 可以查看参数摘要。

## 构建结果

成功后，输出目录中会生成：

```text
ysf_win_cn_dx9.exe
config_cn_dx9.exe
ysfcn.dll
ysfcn.text
font.ttf
release/data_cn.ni
release/data_cn.na
README_移植说明.md
移植报告.json
```

将这些文件复制到 XSEED 语音版游戏根目录即可使用。建议先备份存档和游戏目录。

`移植报告.json` 会记录输入哈希、构建时的 Python／zlib 版本、资源数量、
语音覆盖率、稳定的封包语义 SHA-256、输出文件 SHA-256 和完整封包验证结果。

## 常见错误

### “目录内容不完整，缺少……”

选择的层级不对，或者输入文件没有完整解压。应选择包含游戏 EXE 和 `release` 文件夹的根目录，而不是只选择 `release`。

### “输入版本不匹配”

文件并非构建清单所支持的版本，或曾被其他模组修改。工具会停止以避免生成不可用补丁。

### “输出目录已经存在”

换用一个尚不存在的新目录。构建器不会覆盖现有目录。

### “没有找到 Python 3”

安装 Python 3.10 或更高版本并勾选加入 PATH，或者通过 `-Python` 指定 `python.exe`。

### “封包内容验证通过”，但 `data_cn.ni/.na` 与参考成品字节不同

这不是构建失败。不同 Python 版本可能使用标准 zlib 或 zlib-ng；它们可以把
同一份资源压成不同字节，因此整个 `.ni/.na` 文件的 SHA-256 可能不同。

构建器会另行计算不受压缩实现影响的“封包语义 SHA-256”，覆盖全部 1785 个
条目的路径、顺序、语义元数据和解压内容。只有该强校验与完整解压检查均通过，
成品才会生成。此时压缩字节不同不影响游戏使用；如需调查复现环境，可查看
`移植报告.json` 中记录的 Python 与 zlib 版本。

### 构建失败后出现 `.输出名.building-*`

这是为了诊断而保留的临时目录。确认不再需要其中内容后可以手动删除。

## 构建与验证内容

- 以 XSEED 语音版 `data_us` 的 1785 个条目为底重新生成中文封包；
- 移植 453 个中文 XSO、194 张中文 DDS 和 2 个中文 DAT；
- 为 1920 个语音／旁白调用建立独立中文对应；
- 对主程序、汉化 DLL 和程序内文字地址进行已核验的版本迁移；
- 完整解压检查全部输出条目，并验证大小与 CRC32；
- 使用稳定的语义 SHA-256 核验全部条目的路径、顺序和解压内容；
- 将封包逐字节 SHA-256 作为压缩环境的复现信息，而不是游戏兼容性的判据；
- 不运行游戏 EXE、原汉化补丁 EXE 或任何下载得到的第三方可执行工具。

本工具不能代替实际全流程游戏测试，也不保证能够修正所有问题。

## 权利说明

本次移植分析、构建代码、脚本适配和少量文本修正由 ChatGPT 辅助完成。原游戏、原汉化补丁、字体及其他第三方内容的权利归各自权利人所有。本仓库不提供这些输入内容，也不代表 Falcom、XSEED 或原汉化参与者对本项目的认可。
