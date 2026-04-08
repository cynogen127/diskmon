using System.Windows;
using System.Windows.Threading;

namespace SnipeAgent
{
    public partial class App : Application
    {
        protected override void OnStartup(StartupEventArgs e)
        {
            base.OnStartup(e);

            // Catch any unhandled exception on the UI thread and log it
            // instead of letting Windows swallow it silently (Event ID 1000).
            DispatcherUnhandledException += (_, args) =>
            {
                Log.Write($"UNHANDLED UI EXCEPTION: {args.Exception}");
                MessageBox.Show(
                    $"An unexpected error occurred:\n\n{args.Exception.Message}\n\nDetails have been written to the log file.",
                    "SnipeAgent Error", MessageBoxButton.OK, MessageBoxImage.Error);
                args.Handled = true;   // prevent process crash — remove this line to let it crash-with-log instead
            };

            // Catch unhandled exceptions from Task continuations / async void
            AppDomain.CurrentDomain.UnhandledException += (_, args) =>
            {
                Log.Write($"UNHANDLED DOMAIN EXCEPTION: {args.ExceptionObject}");
            };
        }
    }
}