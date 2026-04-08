using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Threading;
using System.Threading.Tasks;

namespace SnipeAgent
{
    static class AgentInstaller
    {
        /// <summary>
        /// Extracts both PS1 scripts to a temp folder, then runs install-agent.ps1 elevated.
        /// Output lines are streamed via onOutput callback.
        /// </summary>
        public static async Task<bool> RunInstallAsync(
            string serverUrl,
            int pollInterval,
            Action<string> onOutput,
            CancellationToken ct = default)
        {
            // forward to the generic runner with uninstall=false
            return await RunWithFlagsAsync(serverUrl, pollInterval, uninstall: false, onOutput, ct);
        }

        /// <summary>
        /// Executes the installer script in uninstall mode.
        /// </summary>
        public static async Task<bool> RunUninstallAsync(
            Action<string> onOutput,
            CancellationToken ct = default)
        {
            // serverUrl / pollInterval are ignored for uninstall
            return await RunWithFlagsAsync(serverUrl: string.Empty, pollInterval: 0, uninstall: true, onOutput, ct);
        }

        /// <summary>
        /// Executes the installer script with optional uninstall flag.
        /// </summary>
        
        public static async Task<bool> RunWithFlagsAsync(
            string serverUrl,
            int pollInterval,
            bool uninstall,
            Action<string> onOutput,
            CancellationToken ct = default)
        {
            string tempDir    = Path.Combine(Path.GetTempPath(), "DiskHealthAgentInstall");
            Directory.CreateDirectory(tempDir);

            string agentPath   = Path.Combine(tempDir, "DiskHealthAgent.ps1");
            string installPath = Path.Combine(tempDir, "install-agent.ps1");
            string logPath     = Path.Combine(tempDir, "install_out.txt");

            // Write embedded scripts to temp folder
            File.WriteAllText(agentPath,   AgentScripts.Agent,     System.Text.Encoding.UTF8);
            File.WriteAllText(installPath, AgentScripts.Installer, System.Text.Encoding.UTF8);
            File.WriteAllText(logPath, "");

            onOutput($"Scripts extracted to: {tempDir}");
            if (!uninstall)
            {
                onOutput($"Server: {serverUrl}");
                // Pre-flight: warn if the port is unreachable before UAC prompt
                try
                {
                    var uri = new Uri(serverUrl);
                    using var tcp = new TcpClient();
                    bool reachable = tcp.ConnectAsync(uri.Host, uri.Port).Wait(3000);
                    if (!reachable)
                        onOutput($"⚠ Warning: {uri.Host}:{uri.Port} did not respond — double-check the port number before continuing.");
                    else
                        onOutput($"✓ Server {uri.Host}:{uri.Port} is reachable.");
                }
                catch (Exception ex)
                {
                    onOutput($"⚠ Could not verify server connectivity: {ex.Message}");
                }
            }
            onOutput(uninstall ? "Launching uninstaller..." : "Launching installer — UAC prompt will appear, click Yes...\n");

            // Wrapper: run installer and tee output to log file so we can read it back
            string wrapPath = Path.Combine(tempDir, "run.ps1");
            string wrapScript =
                "$ErrorActionPreference = 'Continue'\r\n" +
                "& powershell.exe -ExecutionPolicy Bypass" +
                " -File \"" + installPath + "\"" +
                (uninstall ? " -Uninstall" : " -ServerUrl \"" + serverUrl + "\" -PollInterval " + pollInterval) +
                " *>&1 |" +
                " Tee-Object -FilePath \"" + logPath + "\" -Append\r\n";
            File.WriteAllText(wrapPath, wrapScript, System.Text.Encoding.UTF8);

            var psi = new ProcessStartInfo
            {
                FileName         = "powershell.exe",
                Arguments        = "-ExecutionPolicy Bypass -File \"" + wrapPath + "\"",
                Verb             = "runas",          // UAC elevation
                UseShellExecute  = true,
                WorkingDirectory = tempDir,
            };

            Process? procNullable;
            try
            {
                procNullable = Process.Start(psi);
            }
            catch (Exception ex)
            {
                onOutput("✗ Could not start installer: " + ex.Message);
                if (ex.Message.Contains("cancel") || ex.Message.Contains("denied"))
                    onOutput("  UAC was cancelled or access was denied.");
                return false;
            }

            if (procNullable == null)
            {
                onOutput("✗ Failed to start installer process.");
                return false;
            }

            Process proc = procNullable;   // non-nullable from here on

            // Poll log file for output lines while installer runs
            var seen = new HashSet<string>();
            while (!proc.HasExited)
            {
                await Task.Delay(600, ct);
                FlushLog(logPath, seen, onOutput);
            }
            FlushLog(logPath, seen, onOutput); // final flush

            bool ok = proc.ExitCode == 0;
            onOutput(ok
                ? uninstall ? "\n✓ Agent uninstalled!" : "\n✓ Agent installation complete!"
                : "\n✗ Installer exited with code " + proc.ExitCode);
            return ok;
        }

        private static void FlushLog(string logPath, HashSet<string> seen, Action<string> onOutput)
        {
            try
            {
                if (!File.Exists(logPath)) return;
                var lines = File.ReadAllLines(logPath);
                foreach (var line in lines)
                {
                    string trimmed = line.TrimEnd();
                    if (!string.IsNullOrWhiteSpace(trimmed) && seen.Add(trimmed))
                        onOutput(trimmed);
                }
            }
            catch { /* file may be locked briefly */ }
        }
    }
}