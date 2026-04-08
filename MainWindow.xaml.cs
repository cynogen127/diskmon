using System;
using System.IO;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Media;

namespace SnipeAgent
{
    /// <summary>
    /// Main window for the asset registration application.
    ///
    /// The installer portion now exposes a few command-line switches so that
    /// remote administrators can invoke installs/updates/uninstalls without
    /// interacting with the user interface.
    ///
    /// Supported arguments (order matters):
    ///   -install <serverUrl> [pollInterval]   // normal installation
    ///   -update  <serverUrl> [pollInterval]   // identical to -install, useful when pushing newer exe
    ///   -uninstall                           // remove the agent from the machine
    ///
    /// When a recognised flag is detected the UI will be hidden, the action
    /// performed and the process will exit with code 0 on success or 1 on error.
    /// </summary>
    public partial class MainWindow : Window
    {
        private int          _step      = 1;
        private LicenseInfo? _lic;
        private string       _uniqueId  = "";
        private string       _modelName = "";
        private string       _assetName = "";
        private int          _catId     = Config.DesktopCategoryId;
        private int?         _assetId   = null;
        private string       _fullLog   = "";
        private string       _licenseResult = "Skipped";

        public MainWindow()
        {
            InitializeComponent();
            Log.Init(AppContext.BaseDirectory);
            Api.Logger = msg => AppendAssetLog(msg);
            Loaded += OnLoaded;
        }

        // ── PAGE 1 LOAD ──────────────────────────────────────
        private void OnLoaded(object sender, RoutedEventArgs e)
        {
            // Handle CLI args first — if recognised, the window stays hidden and exits.
            // Must be called here (not in the constructor) so all XAML controls are ready.
            if (ProcessCommandLineArgs()) return;

            HeaderSubtitle.Text = "Register and track this computer";
            _uniqueId  = SysInfo.GetUniqueId();
            _modelName = SysInfo.GetModel();
            _assetName = SysInfo.ComputerName;
            bool laptop = SysInfo.IsLaptop();
            _catId = laptop ? Config.LaptopCategoryId : Config.DesktopCategoryId;

            ValComputerName.Text = SysInfo.ComputerName;
            ValModel.Text        = _modelName;
            ValOs.Text           = SysInfo.GetOs();
            ValCpu.Text          = SysInfo.GetCpu();
            ValType.Text         = laptop ? "Laptop" : "Desktop";
            ValIp.Text           = SysInfo.GetIpAddress();
            ValMac.Text          = SysInfo.GetMacAddress();
            ValRam.Text          = SysInfo.GetRam();
            ValStorage.Text      = SysInfo.GetStorageInfo();
            ValUptime.Text       = SysInfo.GetUptime();
            FooterInfo.Text      = $"{SysInfo.ComputerName}  |  {_uniqueId}  (use -install/-update/-uninstall for automation)";

            Task.Run(() => { string dh = SysInfo.GetDiskHealth(); Dispatcher.BeginInvoke(() => ValDiskHealth.Text = dh); });
            Task.Run(() => { _lic = WindowsLicense.Get(); });
        }

        // ── FOOTER BUTTONS ───────────────────────────────────
        private void BtnNext_Click(object sender, RoutedEventArgs e)
        {
            if (_step == 1) GoToPage2();
            else            Close();
        }

        private void BtnBack_Click(object sender, RoutedEventArgs e)
        {
            if (_step == 2) GoToPage1();
        }

        // ── PAGE 1 ───────────────────────────────────────────
        private void GoToPage1()
        {
            _step = 1;
            Page1.Visibility = Visibility.Visible;
            Page2.Visibility = Visibility.Collapsed;
            Page3.Visibility = Visibility.Collapsed;
            Page4.Visibility = Visibility.Collapsed;
            BtnBack.Visibility  = Visibility.Collapsed;
            BtnNext.Content     = "Next";
            BtnNext.IsEnabled   = true;
            HeaderSubtitle.Text = "Register and track this computer";
            SetStepStyles(1);
            bool agentInstalled = Directory.Exists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "DiskHealthAgent"));
            BtnFooterUninstall.IsEnabled = agentInstalled;
        }

        // ── PAGE 1 → PAGE 2 ──────────────────────────────────
        private void GoToPage2()
        {
            if (string.IsNullOrWhiteSpace(TxtUserName.Text))
            {
                MessageBox.Show("Please enter your name before continuing. [MANDATORY]",
                                "Name Required", MessageBoxButton.OK, MessageBoxImage.Warning);
                TxtUserName.Focus();
                return;
            }

            _step = 2;
            _fullLog = "";
            AssetLogOutput.Text = "";
            LicensePanel.Visibility = Visibility.Collapsed;

            Page1.Visibility = Visibility.Collapsed;
            Page2.Visibility = Visibility.Visible;
            Page3.Visibility = Visibility.Collapsed;
            Page4.Visibility = Visibility.Collapsed;

            BtnBack.Visibility  = Visibility.Visible;
            BtnNext.IsEnabled   = false;
            BtnNext.Content     = "Next";
            Page2Title.Text     = "Registering Asset...";
            AssetStatusText.Text = "Working...";
            AssetStatusBadge.Background = new SolidColorBrush(Color.FromRgb(49, 49, 69));
            AssetStatusText.Foreground  = new SolidColorBrush(Color.FromRgb(144, 144, 176));

            ValDetectedKey.Text = _lic?.ProductKey ?? "Not detected";
            ValActivation.Text  = _lic?.ActivationStatus ?? "Unknown";
            HeaderSubtitle.Text = "Registering asset...";
            SetStepStyles(2);
            bool agentInstalled = Directory.Exists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "DiskHealthAgent"));
            BtnFooterUninstall.IsEnabled = agentInstalled;

            _ = RegisterAssetAsync(TxtUserName.Text.Trim());
        }

        // ── ASSET REGISTRATION ────────────────────────────────
        private async Task RegisterAssetAsync(string userName)
        {
            try
            {
                AppendAssetLog($"Starting registration for: {_assetName}");
                AppendAssetLog($"Connecting to: {Config.ApiUrl}");
                Log.Write($"Registration started — User: {userName}  Computer: {_assetName}");

                AppendAssetLog("Fetching custom field mappings...");
                var fieldMap = await Task.Run(() => SnipeIt.GetFieldMapping());
                AppendAssetLog(fieldMap.Count > 0 ? $"  Found {fieldMap.Count} custom field(s)." : "  Warning: No custom fields returned.");

                var fields = new Dictionary<string, string>();
                void AddField(string friendly, string? value)
                {
                    if (!string.IsNullOrEmpty(value) && fieldMap.TryGetValue(friendly, out string? col))
                        fields[col] = value;
                }

                AddField("IP Address",          SysInfo.GetIpAddress());
                AddField("MAC Address",          SysInfo.GetMacAddress());
                AddField("Operating System",     SysInfo.GetOs());
                AddField("Memory / RAM",         SysInfo.GetRam());
                AddField("Processor / CPU",      SysInfo.GetCpu());
                AddField("Windows Username",     SysInfo.UserName);
                AddField("Total Storage",        SysInfo.GetStorageInfo());
                AddField("Storage Information",  SysInfo.GetStorageInfo());
                AddField("Disk Health",          SysInfo.GetDiskHealth());
                AddField("Uptime",               SysInfo.GetUptime());
                AddField("Staff Name",           userName);
                AddField("Notes",                $"User: {userName}");
                AddField("Default old key",      _lic?.ProductKey);

                AppendAssetLog($"Searching for existing asset [{_uniqueId}]...");
                var existing = await Task.Run(() => SnipeIt.SearchAsset(_uniqueId));

                if (existing != null)
                {
                    int existId = existing["id"]!.GetValue<int>();
                    AppendAssetLog($"Found existing asset (ID: {existId}) — updating...");
                    _assetId = await Task.Run(() => SnipeIt.UpdateAsset(existId, _assetName, fields));
                    AppendAssetLog($"✓ Asset updated (ID: {_assetId})");
                    Log.Write($"Asset updated: {_assetName} ID={_assetId}");
                }
                else
                {
                    AppendAssetLog($"Checking model: {_modelName}");
                    var modelId = await Task.Run(() => SnipeIt.SearchModel(_modelName));

                    if (modelId == null)
                    {
                        AppendAssetLog("Model not found — creating...");
                        modelId = await Task.Run(() => SnipeIt.CreateModel(_modelName, _catId));
                        if (modelId == null)
                        {
                            AppendAssetLog("✗ Failed to create model!");
                            SetAssetBadge(false, "Model creation failed");
                            return;
                        }
                        AppendAssetLog($"✓ Model created (ID: {modelId})");
                    }
                    else AppendAssetLog($"✓ Model found (ID: {modelId})");

                    AppendAssetLog($"Creating asset: {_assetName}...");
                    _assetId = await Task.Run(() => SnipeIt.CreateAsset(modelId.Value, _uniqueId, _assetName, fields));

                    if (_assetId == null)
                    {
                        AppendAssetLog("✗ Failed to create asset!");
                        SetAssetBadge(false, "Asset creation failed");
                        return;
                    }
                    AppendAssetLog($"✓ Asset created (ID: {_assetId})");
                    Log.Write($"Asset created: {_assetName} ID={_assetId}");
                }

                SetAssetBadge(true, $"Done — Asset ID: {_assetId}");
                AppendAssetLog("\nAsset registration complete.");
                AppendAssetLog("Enter a license key below to assign it, or click Skip.");

                _ = Dispatcher.BeginInvoke(() =>
                {
                    Page2Title.Text         = "Asset Registered ✓";
                    LicensePanel.Visibility = Visibility.Visible;
                    BtnBack.Visibility      = Visibility.Collapsed;
                    TxtLicenseKey.Focus();
                });
            }
            catch (Exception ex)
            {
                AppendAssetLog($"\n✗ Error: {ex.Message}");
                Log.Write($"EXCEPTION in RegisterAssetAsync: {ex}");
                SetAssetBadge(false, "Error — see log");
            }
        }

        // ── LICENSE ───────────────────────────────────────────
        private async void BtnAssignLicense_Click(object sender, RoutedEventArgs e)
        {
            string key = TxtLicenseKey.Text.Trim();
            if (string.IsNullOrEmpty(key))
            {
                MessageBox.Show("Please enter a license key, or click Skip.",
                                "Key Required", MessageBoxButton.OK, MessageBoxImage.Information);
                return;
            }
            SetLicenseButtonsEnabled(false);
            await ProcessLicenseAsync(key);
        }

        private void BtnSkipLicense_Click(object sender, RoutedEventArgs e)
        {
            AppendAssetLog("\n— License skipped.");
            Log.Write("License skipped");
            _licenseResult = "Skipped";
            GoToPage3();
        }

        private async Task ProcessLicenseAsync(string licenseKey)
        {
            try
            {
                AppendAssetLog($"\nSearching for license key...");
                var existingLic = await Task.Run(() => SnipeIt.SearchLicense(licenseKey));
                int? licenseId  = null;

                if (existingLic != null)
                {
                    licenseId = existingLic["id"]!.GetValue<int>();
                    AppendAssetLog($"✓ Existing license found (ID: {licenseId})");
                    if (!string.IsNullOrEmpty(_lic?.ProductID))
                        await Task.Run(() => SnipeIt.AddProductIdToNotes(licenseId.Value, _lic.ProductID, _assetName));
                }
                else
                {
                    AppendAssetLog("Creating new license...");
                    licenseId = await Task.Run(() => SnipeIt.CreateLicense(
                        licenseKey, "Windows License", _lic?.ProductID, _lic?.Edition));
                    if (licenseId == null)
                    {
                        AppendAssetLog("✗ Failed to create license.");
                        SetLicenseButtonsEnabled(true);
                        return;
                    }
                    AppendAssetLog($"✓ License created (ID: {licenseId})");
                }

                await Task.Delay(2000);
                AppendAssetLog($"Checking out license {licenseId} to asset {_assetId}...");
                bool ok = await Task.Run(() => SnipeIt.CheckoutLicense(licenseId!.Value, _assetId!.Value));

                if (ok)
                {
                    AppendAssetLog("✓ License assigned!");
                    Log.Write($"License {licenseId} assigned to asset {_assetId}");
                    _licenseResult = $"ID: {licenseId}";
                    GoToPage3();
                }
                else
                {
                    AppendAssetLog("✗ License checkout failed. Try again or skip.");
                    SetLicenseButtonsEnabled(true);
                }
            }
            catch (Exception ex)
            {
                AppendAssetLog($"\n✗ License error: {ex.Message}");
                SetLicenseButtonsEnabled(true);
            }
        }

        // ── COMMAND‑LINE MODE ─────────────────────────────────
        /// <summary>
        /// Returns true if a recognised CLI flag was found (caller should bail out of
        /// normal UI initialisation).  All XAML controls are guaranteed to be ready
        /// because this is called from OnLoaded, never from the constructor.
        /// </summary>
        private bool ProcessCommandLineArgs()
        {
            var args = Environment.GetCommandLineArgs();
            if (args.Length <= 1) return false;

            // hide UI while running unattended
            this.Visibility = Visibility.Hidden;
            AppendAgentLog("Running in command‑line mode...");

            string flag = args[1];
            if (flag.Equals("-uninstall", StringComparison.OrdinalIgnoreCase))
            {
                RunCliAsync(async () =>
                {
                    AppendAgentLog("Starting uninstall...");
                    bool ok = await AgentInstaller.RunUninstallAsync(msg => AppendAgentLog(msg));
                    Application.Current.Shutdown(ok ? 0 : 1);
                });
                return true;
            }

            if (flag.Equals("-install", StringComparison.OrdinalIgnoreCase) ||
                flag.Equals("-update",  StringComparison.OrdinalIgnoreCase))
            {
                string serverUrl = args.Length > 2 ? args[2] : string.Empty;
                int pollInterval = 21600;
                if (args.Length > 3 && int.TryParse(args[3], out int pi)) pollInterval = pi;

                if (string.IsNullOrEmpty(serverUrl))
                {
                    AppendAgentLog("Error: server URL missing.");
                    Application.Current.Shutdown(1);
                    return true;
                }

                RunCliAsync(async () =>
                {
                    AppendAgentLog($"Starting {(flag.Equals("-update", StringComparison.OrdinalIgnoreCase) ? "update" : "install")} to {serverUrl} interval {pollInterval}s...");
                    bool ok = await AgentInstaller.RunInstallAsync(serverUrl, pollInterval, msg => AppendAgentLog(msg));
                    Application.Current.Shutdown(ok ? 0 : 1);
                });
                return true;
            }

            AppendAgentLog($"Unknown command-line argument: {flag}");
            return false;
        }

        /// <summary>
        /// Fires an async CLI action and ensures any unhandled exception is logged
        /// rather than silently crashing the process.
        /// </summary>
        private async void RunCliAsync(Func<Task> action)
        {
            try   { await action(); }
            catch (Exception ex)
            {
                Log.Write($"CLI mode exception: {ex}");
                AppendAgentLog($"✗ Fatal error: {ex.Message}");
                Application.Current.Shutdown(1);
            }
        }

        // ── PAGE 3: AGENT INSTALL ─────────────────────────────
        private void GoToPage3()
        {
            Dispatcher.BeginInvoke(() =>
            {
                _step = 3;
                Page1.Visibility = Visibility.Collapsed;
                Page2.Visibility = Visibility.Collapsed;
                Page3.Visibility = Visibility.Visible;
                Page4.Visibility = Visibility.Collapsed;

                BtnBack.Visibility  = Visibility.Collapsed;
                BtnNext.Content     = "Next";
                BtnNext.IsEnabled   = false;   // enabled only after user acts
                HeaderSubtitle.Text = "Agent installation";
                SetStepStyles(3);

                AgentLogBorder.Visibility  = Visibility.Collapsed;
                AgentLogOutput.Text        = "";
                AgentInputPanel.Visibility = Visibility.Visible;
                TxtAgentServer.Focus();

                // make sure all action buttons are enabled
                BtnInstallAgent.IsEnabled   = true;
                BtnUpdateAgent.IsEnabled    = true;
                BtnSkipAgent.IsEnabled      = true;
                // uninstall only makes sense if the agent appears to be installed on the machine
                bool agentInstalled = Directory.Exists(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), "DiskHealthAgent"));
                BtnUninstallAgent.IsEnabled = agentInstalled;
                BtnFooterUninstall.IsEnabled = agentInstalled;
            });
        }

        private async void BtnInstallAgent_Click(object sender, RoutedEventArgs e)
        {
            await PerformInstallAsync(serverUrl: TxtAgentServer.Text.Trim(),
                                      pollInterval: ParseInterval(),
                                      showLogs: true);
        }

        private async void BtnUpdateAgent_Click(object sender, RoutedEventArgs e)
        {
            // update simply re‑runs install; the script will overwrite existing files
            await PerformInstallAsync(serverUrl: TxtAgentServer.Text.Trim(),
                                      pollInterval: ParseInterval(),
                                      showLogs: true);
        }

        private int ParseInterval()
        {
            if (int.TryParse(TxtPollInterval.Text.Trim(), out int parsed) && parsed >= 60)
                return parsed;
            if (parsed > 0 && parsed < 60)
                MessageBox.Show("Poll interval must be at least 60 seconds. Defaulting to 21600 (6 hours).",
                                "Interval Too Low", MessageBoxButton.OK, MessageBoxImage.Warning);
            return 21600;
        }

        private async Task PerformInstallAsync(string serverUrl, int pollInterval, bool showLogs)
        {
            if (string.IsNullOrEmpty(serverUrl) || !serverUrl.StartsWith("http"))
            {
                MessageBox.Show("Please enter a valid server URL starting with http://",
                                "URL Required", MessageBoxButton.OK, MessageBoxImage.Warning);
                return;
            }

            // disable UI and optionally show log
            AgentInputPanel.Visibility = Visibility.Collapsed;
            AgentLogBorder.Visibility  = Visibility.Visible;
            BtnInstallAgent.IsEnabled  = false;
            BtnUpdateAgent.IsEnabled   = false;
            BtnUninstallAgent.IsEnabled= false;
            BtnSkipAgent.IsEnabled     = false;

            AppendAgentLog($"Installing DiskHealth Agent...");
            AppendAgentLog($"Server: {serverUrl}  |  Poll interval: {pollInterval}s");
            AppendAgentLog("UAC prompt will appear — please click Yes to allow installation.\n");

            bool ok = await AgentInstaller.RunInstallAsync(
                serverUrl, pollInterval,
                msg => AppendAgentLog(msg));

            if (ok)
            {
                AppendAgentLog("\n✓ Agent installed and started successfully.");
                Log.Write($"Agent installed to {serverUrl}");
                GoToPage4(agentStatus: "Installed");
            }
            else
            {
                AppendAgentLog("\n✗ Installation failed or was cancelled.");
                AppendAgentLog("You can try again or skip.");
                _ = Dispatcher.BeginInvoke(() =>
                {
                    AgentInputPanel.Visibility = Visibility.Visible;
                    BtnInstallAgent.IsEnabled  = true;
                    BtnUpdateAgent.IsEnabled   = true;
                    BtnUninstallAgent.IsEnabled= true;
                    BtnSkipAgent.IsEnabled     = true;
                });
            }
        }

        private void BtnSkipAgent_Click(object sender, RoutedEventArgs e)
        {
            Log.Write("Agent install skipped");
            GoToPage4(agentStatus: "Skipped");
        }

        private async void BtnUninstallAgent_Click(object sender, RoutedEventArgs e)
        {
            await StartUninstallFlow();
        }

        private async void BtnFooterUninstall_Click(object sender, RoutedEventArgs e)
        {
            await StartUninstallFlow();
        }

        /// <summary>
        /// Common logic to perform an agent uninstall, usable from multiple UI locations.
        /// </summary>
        private async Task StartUninstallFlow()
        {
            if (MessageBox.Show("Are you sure you want to uninstall the DiskHealth Agent?", "Confirm", MessageBoxButton.YesNo, MessageBoxImage.Question) != MessageBoxResult.Yes)
                return;

            // navigate to installation page so log is visible
            GoToPage3();
            AgentInputPanel.Visibility = Visibility.Collapsed;
            AgentLogBorder.Visibility  = Visibility.Visible;
            BtnInstallAgent.IsEnabled  = false;
            BtnUpdateAgent.IsEnabled   = false;
            BtnUninstallAgent.IsEnabled= false;
            BtnSkipAgent.IsEnabled     = false;
            BtnFooterUninstall.IsEnabled = false;

            AppendAgentLog("Uninstalling DiskHealth Agent...\n");
            bool ok = await AgentInstaller.RunUninstallAsync(msg => AppendAgentLog(msg));
            if (ok)
            {
                AppendAgentLog("\n✓ Agent removed.");
                GoToPage4(agentStatus: "Uninstalled");
            }
            else
            {
                AppendAgentLog("\n✗ Uninstall failed.");
                _ = Dispatcher.BeginInvoke(() =>
                {
                    AgentInputPanel.Visibility = Visibility.Visible;
                    BtnInstallAgent.IsEnabled  = true;
                    BtnUpdateAgent.IsEnabled   = true;
                    BtnUninstallAgent.IsEnabled= true;
                    BtnSkipAgent.IsEnabled     = true;
                    BtnFooterUninstall.IsEnabled = true;
                });
            }
        }

        // ── PAGE 4: COMPLETE ──────────────────────────────────
        private void GoToPage4(string agentStatus)
        {
            Dispatcher.BeginInvoke(() =>
            {
                _step = 4;
                Page1.Visibility = Visibility.Collapsed;
                Page2.Visibility = Visibility.Collapsed;
                Page3.Visibility = Visibility.Collapsed;
                Page4.Visibility = Visibility.Visible;

                BtnBack.Visibility  = Visibility.Collapsed;
                BtnNext.Content     = "Finish";
                BtnNext.IsEnabled   = true;
                HeaderSubtitle.Text = "All done!";
                SetStepStyles(4);

                SumAssetId.Text  = _assetId?.ToString() ?? "—";
                SumComputer.Text = _assetName;
                SumLicense.Text  = _licenseResult;
                SumAgent.Text    = agentStatus;

                bool success = _assetId != null;
                FinalBanner.Background = new SolidColorBrush(success
                    ? Color.FromRgb(20, 58, 36) : Color.FromRgb(58, 20, 20));
                FinalIcon.Text = success ? "\u2705" : "\u274C";
                FinalTitle.Text = success ? "All Done!" : "Registration Failed";
                FinalTitle.Foreground = new SolidColorBrush(success
                    ? Color.FromRgb(34, 197, 94) : Color.FromRgb(239, 68, 68));
                FinalSub.Text = success
                    ? $"Asset '{_assetName}' (ID: {_assetId}) registered. License: {_licenseResult}. Agent: {agentStatus}."
                    : "Check the log below for details.";

                // Combine asset log + agent log for final view
                FinalLogOutput.Text = _fullLog + "\n" + AgentLogOutput.Text;
                FinalLogScroller.ScrollToBottom();

                Log.Write($"Complete — Asset={_assetId} License={_licenseResult} Agent={agentStatus}");
            });
        }

        // ── HELPERS ───────────────────────────────────────────
        private void AppendAssetLog(string message)
        {
            _fullLog += message + "\n";
            Dispatcher.BeginInvoke(() =>
            {
                AssetLogOutput.Text += message + "\n";
                AssetLogScroller.ScrollToBottom();
            });
        }

        private void AppendAgentLog(string message)
        {
            Dispatcher.BeginInvoke(() =>
            {
                AgentLogOutput.Text += message + "\n";
                AgentLogScroller.ScrollToBottom();
            });
        }

        private void SetAssetBadge(bool success, string text)
        {
            Dispatcher.BeginInvoke(() =>
            {
                AssetStatusBadge.Background = new SolidColorBrush(success
                    ? Color.FromRgb(20, 58, 36) : Color.FromRgb(58, 20, 20));
                AssetStatusText.Foreground = new SolidColorBrush(success
                    ? Color.FromRgb(34, 197, 94) : Color.FromRgb(239, 68, 68));
                AssetStatusText.Text = text;
            });
        }

        private void SetLicenseButtonsEnabled(bool enabled)
        {
            Dispatcher.BeginInvoke(() =>
            {
                TxtLicenseKey.IsEnabled    = enabled;
                BtnAssignLicense.IsEnabled = enabled;
                BtnSkipLicense.IsEnabled   = enabled;
            });
        }

        private void SetStepStyles(int active)
        {
            var purple = new SolidColorBrush(Color.FromRgb(124, 58, 237));
            var grey   = new SolidColorBrush(Color.FromRgb(61,  61,  92));
            var white  = new SolidColorBrush(Colors.White);
            var dim    = new SolidColorBrush(Color.FromRgb(112, 112, 160));
            var bright = new SolidColorBrush(Color.FromRgb(240, 240, 255));

            Step1Circle.Background = active >= 1 ? purple : grey;
            Step1Num.Foreground    = active >= 1 ? white  : dim;
            Step1Label.Foreground  = active >= 1 ? bright : dim;

            Step2Circle.Background = active >= 2 ? purple : grey;
            Step2Num.Foreground    = active >= 2 ? white  : dim;
            Step2Label.Foreground  = active >= 2 ? bright : dim;

            Step3Circle.Background = active >= 3 ? purple : grey;
            Step3Num.Foreground    = active >= 3 ? white  : dim;
            Step3Label.Foreground  = active >= 3 ? bright : dim;

            Step4Circle.Background = active >= 4 ? purple : grey;
            Step4Num.Foreground    = active >= 4 ? white  : dim;
            Step4Label.Foreground  = active >= 4 ? bright : dim;
        }
    }
}