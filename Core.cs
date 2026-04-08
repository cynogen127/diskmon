using System.IO;
using System.Management;
using System.Net.Http;
using System.Net.NetworkInformation;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading;
using Microsoft.Win32;

namespace SnipeAgent
{
    // ─────────────────────────────────────────────────────────────
    //  CONFIGURATION  – edit before building
    // ─────────────────────────────────────────────────────────────
    static class Config
    {
        public const string ApiUrl  = "http://192.168.0.150:8000/api/v1";
        public const string ApiToken = "eyJ0eXAiOiJKV1QiLCJhbGciOiJSUzI1NiJ9.eyJhdWQiOiIxIiwianRpIjoiNDkzNDUzZWIxMWQzYjJiMTA0MmU0MTBlYjRkNTczNzI3OWU3ZDMwMjk2OWU0MDY1NzYyYmRlYTRjMGQ2YjA3YzVlZDZmZGVhMmQ1NTRmOGEiLCJpYXQiOjE3Njg3ODcyOTMuNjI5NjUyLCJuYmYiOjE3Njg3ODcyOTMuNjI5NjUzLCJleHAiOjMwMzEwOTEyOTMuNjI4MjIyLCJzdWIiOiIxIiwic2NvcGVzIjpbXX0.dxzHKJx1brOPZkV_f5gVw3odPyO2ww115hbOLKtfcHNS4tnzY7j6w-inCx8Qm5u1IYF0soFcd-OIHuL0U8v9cZxVDKkZPeiyaELkm40GMEYH-PIOP_bMnqeG40nZQMWZPbqPqNlW4GI72jYtxuJ06NSwcMCrboUNnzUwu2AB2qtjGHllyL_fyEyDg3BX7aCMNK4PaAPn5Fp-I-Y-7bKRTnCCRMOzTD1DzZZPPhD6dpdSvJzBgspQ4ciZKwbSF6PnGhpDrJK9i9FSpbz8Qv4xNeW_Dhk7ImZUSDyGaa-_C_C9lkx56hSIVxKzjUPpoIei2zvkUC7_k-4KWwDPVHAiK4vnoHZBDZjmIwr7rrM1yzjfTZZbwSN39S4G6J6TTSfhHB4B3lXV98mXb6VUHCgCui9J9RpD625nB_4u6EZ5An8GKKG8KeHvVWFydee1NEuBMiViGdO2abD6mvs7NhtMNKB96eAYlL4W2BMpsa0mBlGh31R-rJm42CSJbYOXUKKfgc2ZuBTLSOzddLm6O8I2s3FAZkIyu60odUUgm3vz2CyJRmJmkaOqwcQdrLASSLmXSMcSrah52iRw2kDOeWCKumYoq4frO-Z08nnxkyp05PUK36CyrvWBbFyoCDTnYTWLpPyv_z2d22275SnOoIp2GV8bHY5MU-ccL350_149rss";

        public const int StatusId             = 2;
        public const int WindowsLicenseCatId  = 2;
        public const int LaptopCategoryId     = 2;
        public const int DesktopCategoryId    = 3;
    }

    // ─────────────────────────────────────────────────────────────
    //  LOGGING
    // ─────────────────────────────────────────────────────────────
    static class Log
    {
        private static string _filePath = "";
        public static string FilePath => _filePath;

        public static void Init(string directory)
        {
            string date = DateTime.Now.ToString("ddMMyyyy");
            _filePath = System.IO.Path.Combine(directory, $"log-{date}.log");
            Directory.CreateDirectory(directory);
        }

        public static void Write(string message)
        {
            string line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} - {message}";
            try { File.AppendAllText(_filePath, line + Environment.NewLine, Encoding.UTF8); }
            catch { }
        }
    }

    // ─────────────────────────────────────────────────────────────
    //  SNIPE-IT API CLIENT
    // ─────────────────────────────────────────────────────────────
    static class Api
    {
        private static readonly HttpClient _http = new();

        static Api()
        {
            _http.DefaultRequestHeaders.Add("Authorization", $"Bearer {Config.ApiToken}");
            _http.DefaultRequestHeaders.Add("Accept", "application/json");
            _http.Timeout = TimeSpan.FromSeconds(30);
        }

        public static Action<string>? Logger { get; set; }

        public static JsonNode? Get(string endpoint)    => Send(HttpMethod.Get,   endpoint, null);
        public static JsonNode? Post(string endpoint, object body)  => Send(HttpMethod.Post,  endpoint, body);
        public static JsonNode? Patch(string endpoint, object body) => Send(HttpMethod.Patch, endpoint, body);

        private static JsonNode? Send(HttpMethod method, string endpoint, object? body)
        {
            string url = $"{Config.ApiUrl}{endpoint}";
            Logger?.Invoke($"→ {method} {url}");
            try
            {
                var req = new HttpRequestMessage(method, url);
                if (body != null)
                    req.Content = new StringContent(JsonSerializer.Serialize(body), Encoding.UTF8, "application/json");

                var resp = _http.Send(req);
                string raw = resp.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                if (!resp.IsSuccessStatusCode)
                    Logger?.Invoke($"  HTTP {(int)resp.StatusCode}: {raw}");
                return JsonNode.Parse(raw);
            }
            catch (Exception ex)
            {
                Logger?.Invoke($"  ERROR: {ex.Message}");
                return null;
            }
        }
    }

    // ─────────────────────────────────────────────────────────────
    //  WMI HELPER
    // ─────────────────────────────────────────────────────────────
    static class Wmi
    {
        public static List<Dictionary<string, object?>> Query(string wmiClass, string? ns = null)
        {
            var results = new List<Dictionary<string, object?>>();
            try
            {
                using var searcher = new ManagementObjectSearcher(ns ?? "root\\cimv2", $"SELECT * FROM {wmiClass}");
                foreach (ManagementObject obj in searcher.Get())
                {
                    var row = new Dictionary<string, object?>();
                    foreach (var prop in obj.Properties)
                        row[prop.Name] = prop.Value;
                    results.Add(row);
                }
            }
            catch { }
            return results;
        }

        public static Dictionary<string, object?> First(string wmiClass, string? ns = null)
            => Query(wmiClass, ns).FirstOrDefault() ?? new();

        public static T? Get<T>(Dictionary<string, object?> row, string key)
        {
            if (row.TryGetValue(key, out var val) && val is T typed) return typed;
            return default;
        }
    }

    // ─────────────────────────────────────────────────────────────
    //  SYSTEM INFO
    // ─────────────────────────────────────────────────────────────
    static class SysInfo
    {
        public static string ComputerName => Environment.MachineName;
        public static string UserName     => Environment.UserName;

        public static string GetUniqueId()
        {
            foreach (var disk in Wmi.Query("Win32_PhysicalMedia"))
            {
                string? serial = Wmi.Get<string>(disk, "SerialNumber")?.Trim();
                if (!string.IsNullOrEmpty(serial)) return $"{ComputerName}-{serial}";
            }
            return ComputerName;
        }

        public static string GetModel()
        {
            var cs = Wmi.First("Win32_ComputerSystem");
            string mfr   = Wmi.Get<string>(cs, "Manufacturer") ?? "";
            string model = Wmi.Get<string>(cs, "Model") ?? "";
            return string.IsNullOrEmpty(model) ? "Generic Desktop" : $"{mfr} {model}".Trim();
        }

        public static bool IsLaptop()
        {
            if (Wmi.Query("Win32_Battery").Count > 0) return true;
            var enc = Wmi.First("Win32_SystemEnclosure");
            if (enc.TryGetValue("ChassisTypes", out var ct) && ct is ushort[] types && types.Length > 0)
            {
                int[] laptopTypes  = { 8, 9, 10, 11, 14, 18, 21, 31, 32 };
                int[] desktopTypes = { 3, 4, 5, 6, 7, 13, 15, 16 };
                if (laptopTypes.Contains(types[0]))  return true;
                if (desktopTypes.Contains(types[0])) return false;
            }
            return false;
        }

        public static string GetIpAddress()
        {
            foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (ni.OperationalStatus != OperationalStatus.Up) continue;
                foreach (var ua in ni.GetIPProperties().UnicastAddresses)
                {
                    if (ua.Address.AddressFamily == System.Net.Sockets.AddressFamily.InterNetwork)
                    {
                        string ip = ua.Address.ToString();
                        if (!ip.StartsWith("169.") && ip != "127.0.0.1") return ip;
                    }
                }
            }
            return "";
        }

        public static string GetMacAddress()
        {
            foreach (var ni in NetworkInterface.GetAllNetworkInterfaces())
            {
                if (ni.OperationalStatus != OperationalStatus.Up) continue;
                string mac = ni.GetPhysicalAddress().ToString();
                if (mac.Length == 12)
                    return string.Join(":", Enumerable.Range(0, 6).Select(i => mac.Substring(i * 2, 2)));
            }
            return "";
        }

        public static string GetOs()
        {
            var os = Wmi.First("Win32_OperatingSystem");
            return $"{Wmi.Get<string>(os, "Caption")} (v{Wmi.Get<string>(os, "Version")})";
        }

        public static string GetRam()
        {
            var cs = Wmi.First("Win32_ComputerSystem");
            if (cs.TryGetValue("TotalPhysicalMemory", out var v) && v != null)
                return $"{Math.Round(Convert.ToDouble(v) / (1024 * 1024 * 1024), 2)} GB";
            return "";
        }

        public static string GetCpu()
        {
            var cpu = Wmi.First("Win32_Processor");
            return Wmi.Get<string>(cpu, "Name") ?? "";
        }

        public static string GetStorageInfo()
        {
            var parts = new List<string>();
            foreach (var disk in Wmi.Query("Win32_LogicalDisk")
                .Where(d => d.GetValueOrDefault("DriveType") is uint dt && dt == 3))
            {
                string id   = Wmi.Get<string>(disk, "DeviceID") ?? "";
                double size = disk.TryGetValue("Size",      out var s) && s != null ? Convert.ToDouble(s) / (1024.0 * 1024 * 1024) : 0;
                double free = disk.TryGetValue("FreeSpace", out var f) && f != null ? Convert.ToDouble(f) / (1024.0 * 1024 * 1024) : 0;
                double used = size - free;
                double pct  = size > 0 ? Math.Round(used / size * 100, 1) : 0;
                parts.Add($"{id} {Math.Round(used, 2)}GB / {Math.Round(size, 2)}GB ({pct}%)");
            }
            return string.Join("  |  ", parts);
        }

        public static string GetDiskHealth()
        {
            var parts = new List<string>();
            try
            {
                foreach (var disk in Wmi.Query("MSFT_PhysicalDisk", @"root\microsoft\windows\storage"))
                {
                    string name    = Wmi.Get<string>(disk, "FriendlyName") ?? "Unknown";
                    ushort health  = disk.TryGetValue("HealthStatus", out var h) && h != null ? Convert.ToUInt16(h) : (ushort)1;
                    ushort media   = disk.TryGetValue("MediaType",    out var m) && m != null ? Convert.ToUInt16(m) : (ushort)0;
                    double sizeGb  = disk.TryGetValue("Size",         out var s) && s != null ? Convert.ToDouble(s) / (1024.0 * 1024 * 1024) : 0;
                    string hs = health switch { 0 => "Healthy", 1 => "Warning", 2 => "Unhealthy", _ => "Unknown" };
                    string ms = media  switch { 3 => "HDD", 4 => "SSD", 5 => "SCM", _ => "Unspecified" };
                    parts.Add($"{name} ({ms}, {Math.Round(sizeGb, 2)} GB) - {hs}");
                }
            }
            catch { }
            return parts.Count > 0 ? string.Join(" | ", parts) : "Unknown";
        }

        public static string GetUptime()
        {
            var os = Wmi.First("Win32_OperatingSystem");
            if (os.TryGetValue("LastBootUpTime", out var boot) && boot is string bootStr)
            {
                try
                {
                    var uptime = DateTime.Now - ManagementDateTimeConverter.ToDateTime(bootStr);
                    if (uptime.Days > 0)  return $"{uptime.Days}d {uptime.Hours}h {uptime.Minutes}m";
                    if (uptime.Hours > 0) return $"{uptime.Hours}h {uptime.Minutes}m";
                    return $"{uptime.Minutes}m";
                }
                catch { }
            }
            return "";
        }
    }

    // ─────────────────────────────────────────────────────────────
    //  WINDOWS LICENSE
    // ─────────────────────────────────────────────────────────────
    record LicenseInfo(string? ProductKey, string? ProductID, string? Edition,
                       string? ActivationStatus, string? LicenseType);

    static class WindowsLicense
    {
        public static LicenseInfo Get()
        {
            string? productKey = null, productId = null, edition = null,
                    activationStatus = null;
            try
            {
                foreach (var p in Wmi.Query("SoftwareLicensingProduct"))
                {
                    string? partialKey = Wmi.Get<string>(p, "PartialProductKey");
                    string? name       = Wmi.Get<string>(p, "Name");
                    if (string.IsNullOrEmpty(partialKey) || name == null || !name.Contains("Windows")) continue;
                    productId        = Wmi.Get<string>(p, "ProductKeyID");
                    edition          = name;
                    uint status      = p.TryGetValue("LicenseStatus", out var ls) && ls != null ? Convert.ToUInt32(ls) : 0;
                    activationStatus = status == 1 ? "Licensed" : "Unlicensed";
                    if (string.IsNullOrEmpty(productKey))
                        productKey = "XXXXX-XXXXX-XXXXX-XXXXX-" + partialKey;
                    break;
                }

                // Try SoftwareProtectionPlatform
                using (var key = Microsoft.Win32.Registry.LocalMachine
                    .OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion\SoftwareProtectionPlatform"))
                {
                    string? backup = key?.GetValue("BackupProductKeyDefault") as string;
                    if (!string.IsNullOrEmpty(backup) && !backup.StartsWith("BBBBB"))
                    { productKey = backup; goto done; }
                }

                // Try OEM
                using (var key = Microsoft.Win32.Registry.LocalMachine
                    .OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\OEMInformation"))
                {
                    string? oemKey = key?.GetValue("ProductKey") as string;
                    if (!string.IsNullOrEmpty(oemKey) && !oemKey.StartsWith("BBBBB"))
                    { productKey = oemKey; goto done; }
                }

                string? decoded = DecodeFromRegistry();
                if (!string.IsNullOrEmpty(decoded)) productKey = decoded;

                done:;
            }
            catch { }
            return new LicenseInfo(productKey, productId, edition, activationStatus, "Retail/OEM");
        }

        private static string? DecodeFromRegistry()
        {
            try
            {
                using var key = Microsoft.Win32.Registry.LocalMachine
                    .OpenSubKey(@"SOFTWARE\Microsoft\Windows NT\CurrentVersion");
                if (key?.GetValue("DigitalProductId") is not byte[] id) return null;

                int keyOffset = 52;
                int isWin8    = (id[66] / 6) & 1;
                id[66]        = (byte)((id[66] & 0xF7) | ((isWin8 & 2) * 4));

                const string chars = "BCDFGHJKMPQRTVWXY2346789";
                string raw = "";
                for (int i = 24; i >= 0; i--)
                {
                    int cur = 0;
                    for (int j = 14; j >= 0; j--)
                    {
                        cur = cur * 256 + id[j + keyOffset];
                        id[j + keyOffset] = (byte)(cur / 24);
                        cur %= 24;
                    }
                    raw = chars[cur] + raw;
                }

                var sb = new StringBuilder();
                for (int i = 0; i < 25; i++)
                {
                    sb.Append(raw[i]);
                    if ((i + 1) % 5 == 0 && i != 24) sb.Append('-');
                }
                string result = sb.ToString();
                return result.Replace("-", "").Distinct().Count() <= 1 ? null : result;
            }
            catch { return null; }
        }
    }

    // ─────────────────────────────────────────────────────────────
    //  SNIPE-IT OPERATIONS
    // ─────────────────────────────────────────────────────────────
    static class SnipeIt
    {
        private static int? ExtractId(JsonNode? resp) =>
            resp?["payload"]?["id"]?.GetValue<int>() ?? resp?["id"]?.GetValue<int>();

        public static Dictionary<string, string> GetFieldMapping()
        {
            var map  = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
            var resp = Api.Get("/fields");
            if (resp?["rows"] is JsonArray rows)
                foreach (var r in rows)
                {
                    string? name = r?["name"]?.GetValue<string>();
                    string? col  = r?["db_column_name"]?.GetValue<string>();
                    if (!string.IsNullOrEmpty(name) && !string.IsNullOrEmpty(col))
                        map[name] = col;
                }
            return map;
        }

        public static JsonNode? SearchAsset(string serial)
        {
            var resp = Api.Get($"/hardware?search={Uri.EscapeDataString(serial)}");
            if (resp?["rows"] is JsonArray rows)
                foreach (var r in rows)
                    if (r?["serial"]?.GetValue<string>() == serial) return r;
            return null;
        }

        public static int? CreateAsset(int modelId, string serial, string name,
                                        Dictionary<string, string> fields, string notes = "")
        {
            var body = new Dictionary<string, object?>
            {
                ["model_id"]  = modelId,
                ["status_id"] = Config.StatusId,
                ["serial"]    = serial,
                ["asset_tag"] = null,
                ["name"]      = name,
                ["notes"]     = notes
            };
            foreach (var kv in fields) body[kv.Key] = kv.Value;
            var resp = Api.Post("/hardware", body);
            int? id  = ExtractId(resp);
            if (id != null) return id;

            // Duplicate? Find existing
            if (resp?["messages"]?["serial"] != null || resp?["messages"]?["asset_tag"] != null)
            {
                Thread.Sleep(1000);
                return SearchAsset(serial)?["id"]?.GetValue<int>();
            }
            return null;
        }

        public static int? UpdateAsset(int assetId, string name,
                                        Dictionary<string, string> fields, string notes = "")
        {
            var body = new Dictionary<string, object?> { ["name"] = name, ["notes"] = notes };
            foreach (var kv in fields) body[kv.Key] = kv.Value;
            var resp = Api.Patch($"/hardware/{assetId}", body);
            return ExtractId(resp) ?? assetId;
        }

        public static int? SearchModel(string name)
        {
            var resp = Api.Get($"/models?search={Uri.EscapeDataString(name)}");
            if (resp?["rows"] is JsonArray rows)
                foreach (var r in rows)
                    if (r?["name"]?.GetValue<string>() == name)
                        return r["id"]!.GetValue<int>();
            return null;
        }

        public static int? CreateModel(string name, int categoryId)
        {
            var resp = Api.Post("/models", new { name, category_id = categoryId });
            int? id  = ExtractId(resp);
            if (id != null) return id;
            if (resp?["status"]?.GetValue<string>() == "success")
            { Thread.Sleep(2000); return SearchModel(name); }
            return null;
        }

        public static JsonNode? SearchLicense(string productKey)
        {
            foreach (string ep in new[] { $"/licenses?search={Uri.EscapeDataString(productKey)}", "/licenses?limit=200" })
            {
                var resp = Api.Get(ep);
                if (resp?["rows"] is JsonArray rows)
                    foreach (var r in rows)
                        if (r?["product_key"]?.GetValue<string>() == productKey) return r;
            }
            return null;
        }

        public static int? CreateLicense(string productKey, string productName,
                                          string? productId, string? edition, int seats = 1)
        {
            string notes = $"Product ID: {productId}";
            if (!string.IsNullOrEmpty(edition)) notes += $"\nEdition: {edition}";

            var resp = Api.Post("/licenses", new Dictionary<string, object?>
            {
                ["name"]         = productName,
                ["product_key"]  = productKey,
                ["serial"]       = productKey,
                ["seats"]        = seats,
                ["category_id"]  = Config.WindowsLicenseCatId,
                ["notes"]        = notes,
                ["maintained"]   = false,
                ["reassignable"] = true
            });
            int? id = ExtractId(resp);
            if (id != null) return id;
            if (resp?["status"]?.GetValue<string>() == "success")
            { Thread.Sleep(2000); return SearchLicense(productKey)?["id"]?.GetValue<int>(); }
            return null;
        }

        public static void UpdateLicenseSeats(int licenseId, int seats) =>
            Api.Patch($"/licenses/{licenseId}", new { seats });

        public static void AddProductIdToNotes(int licenseId, string productId, string assetName)
        {
            var resp    = Api.Get($"/licenses/{licenseId}");
            string cur  = resp?["notes"]?.GetValue<string>() ?? "";
            if (cur.Contains(productId)) return;
            string updated = string.IsNullOrWhiteSpace(cur)
                ? $"[{assetName}] Product ID: {productId}"
                : cur + $"\n[{assetName}] Product ID: {productId}";
            Api.Patch($"/licenses/{licenseId}", new { notes = updated });
        }

        public static bool CheckoutLicense(int licenseId, int assetId)
        {
            var licResp  = Api.Get($"/licenses/{licenseId}");
            int total    = licResp?["seats"]?.GetValue<int>() ?? 0;
            int avail    = licResp?["free_seats_count"]?.GetValue<int>()
                        ?? licResp?["remaining"]?.GetValue<int>() ?? 0;

            int? seatId  = null;
            bool hasIt   = false;

            var seatsResp = Api.Get($"/licenses/{licenseId}/seats");
            if (seatsResp?["rows"] is JsonArray rows)
                foreach (var seat in rows)
                {
                    int? sid        = seat?["id"]?.GetValue<int>();
                    int? assignedId = seat?["assigned_asset"]?["id"]?.GetValue<int>();
                    if (assignedId == assetId) { hasIt = true; }
                    if (seatId == null && assignedId == null && sid != null) seatId = sid;
                }

            if (hasIt) { Api.Logger?.Invoke("  ✓ License already assigned"); return true; }

            if (avail == 0)
            {
                Api.Logger?.Invoke($"  ⚠ No seats, expanding to {total + 1}...");
                UpdateLicenseSeats(licenseId, total + 1);
                Thread.Sleep(2000);

                // Re-fetch seat
                var fresh = Api.Get($"/licenses/{licenseId}/seats");
                if (fresh?["rows"] is JsonArray fr)
                    foreach (var seat in fr)
                    {
                        int? sid        = seat?["id"]?.GetValue<int>();
                        int? assignedId = seat?["assigned_asset"]?["id"]?.GetValue<int>();
                        if (seatId == null && assignedId == null && sid != null) seatId = sid;
                    }
            }

            if (seatId == null) { Api.Logger?.Invoke("  ✗ No available seat found"); return false; }

            // Snipe-IT: use asset_id for asset checkout, not assigned_to (user field)
            var resp = Api.Patch($"/licenses/{licenseId}/seats/{seatId}",
                       new { asset_id = assetId });

            if (resp?["status"]?.GetValue<string>() == "error")
            { Api.Logger?.Invoke($"  ✗ Checkout error: {resp["messages"]}"); return false; }

            return true;
        }
    }
}