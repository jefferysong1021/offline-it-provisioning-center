# 离线 IT 装机与办公环境自动化初始化工具

IT Provisioning Center

面向企业 IT 装机和账号配置场景的本地桌面原型。

## 启动

```powershell
python app.py
```

验证环境：

```powershell
python app.py --self-test
```

## 邮箱配置

邮箱页支持 QQ、163、Gmail 和自定义服务商预设。输入邮箱和授权码后，应用会自动填充 POP/SMTP/IMAP 参数，并可生成 Thunderbird XML 配置预览。

安全边界：

- 授权码只在内存中使用；
- 不写入 JSON、日志或安装命令；
- QQ/163 等服务商通常需要客户端授权码；
- 当前版本生成客户端配置文件，不强行修改 Outlook/Windows Mail/Thunderbird 的真实用户 Profile；
- 真实客户端自动配置需要按具体客户端和企业策略接入。

## 软件和驱动

将安装包放在：

```text
it-provisioning-center/
├── app.py
├── packages/
├── drivers/
└── package_manifest.json
```

复制 `package_manifest.example.json` 为 `package_manifest.json`，修改文件名和静默参数。也可以不配置清单，程序会扫描 `packages/` 和 `drivers/` 中的 `.exe/.msi/.msix/.inf/.ps1/.bat/.cmd` 文件。

如果存在 `package_manifest.json`，程序现在会将“清单项目”和“目录中自动发现的项目”合并，不会因为示例清单而停止扫描。也支持直接把 `.exe/.msi/.msix/.msixbundle/.appx/.inf/.cab` 放在程序 EXE 同级目录。

双击软件行可以选择，再点击“安装选中项”。安装前会：

1. 检查文件是否位于程序目录内；
2. 校验可选的 SHA-256；
3. 显示管理员权限要求；
4. 按清单执行并等待退出码；
5. 将结果写入 `logs/`。

请只放入来源可信、已验证的安装包。驱动和脚本执行前应先在测试电脑验证。

## 界面截图

### 邮箱配置 / Mail Profile

![邮箱配置界面](docs/screenshots/mail-profile.png)

### 软件与驱动 / Packages

![软件与驱动安装界面](docs/screenshots/packages.png)

## 打包为 U 盘便携版

在 Windows 上使用与目标电脑架构一致的 Python，安装 PyInstaller：

```powershell
py -m pip install pyinstaller
```

然后在当前目录运行：

```powershell
.\build_portable.ps1
```

输出目录：

```text
dist\ITProvisioningCenter\
├── ITProvisioningCenter.exe
├── packages\
├── drivers\
├── logs\
└── package_manifest.json
```

把整个 `dist\ITProvisioningCenter` 文件夹复制到 U 盘。建议使用 `--onedir`，不要使用 `--onefile`，因为安装包、驱动和日志需要与程序保持在同一目录结构中。
