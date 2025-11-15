# Kyocera Solar CLI

A beautiful command-line interface for monitoring your Kyocera solar power system in real-time.

## Features

- 🌇 Real-time solar generation, grid import/export, battery status, and home consumption
- 🔋 Dynamic battery indicator (🔋 when charged, 🪫 when at reserve level)
- ⚡ Estimated time remaining until battery reaches reserve or full charge
- 🌱 Clean energy percentage with visual progress bars
- 📊 Lifetime generation and CO₂ savings statistics
- 🌤️ Local weather information
- 🔄 Auto-refresh watch mode
- 🎨 Color-coded output with emojis for quick visual scanning

## Screenshot

```
🌇 Kyocera Solar
Saturday, November 15 · 11:14 AM
☀️  中野区 · 16°C

🔆 Solar             1.4 kW
⚡ Grid             +0.2 kW
🔋 Battery          +0.7 kW  [████░░░░░░]  41% (~14h to 100%)
🏡 Home              0.5 kW  [██████████] 100% 🌱

Lifetime: 46.5 kWh generated · 1.14 kg CO₂ saved
Battery: 16.5 kWh total · 11.6 kWh usable · 30% reserve
Made by Jordy Meow (https://jordymeow.com)
⟳ Refreshing every 30s · Press Ctrl+C to stop
```

## Installation

1. Clone the repository:
```bash
git clone https://github.com/jordymeow/kyocera-solar-cli.git
cd kyocera-solar-cli
```

2. Copy the example configuration and edit it with your credentials:
```bash
cp kyocera.conf.example kyocera.conf
# Edit kyocera.conf with your Kyocera portal credentials
```

3. Run the CLI:
```bash
python3 kyocera_cli.py
```

## Usage

### Basic Usage

Display current solar system status:
```bash
python3 kyocera_cli.py
```

### Watch Mode

Auto-refresh display every 30 seconds (default):
```bash
python3 kyocera_cli.py -w
```

Custom refresh interval (in seconds):
```bash
python3 kyocera_cli.py -w --interval 10
```

### JSON Output

Get raw JSON data:
```bash
python3 kyocera_cli.py --json
```

### Command Line Options

```
-w, --watch              Watch mode: continuously refresh the display
--interval SECONDS       Refresh interval in seconds for watch mode (default: 30)
--json                   Output raw JSON data instead of formatted display
--config PATH            Path to configuration file (default: ./kyocera.conf)
--force-login            Force fresh login (ignore cached session)
-v, --verbose            Increase logging verbosity (use -vv for debug)
```

## Configuration

The configuration file (`kyocera.conf`) contains your Kyocera portal credentials and battery settings:

```ini
[auth]
email = your-email@example.com
password = your-password

[site]
organization_id = 1
site_id = 123456
location = Japan

[battery]
# Battery capacity in kWh (adjust for your system)
capacity_kwh = 16.5
# Minimum reserve percentage
reserve_percent = 30
```

### Finding Your Site ID

1. Log in to your Kyocera solar portal
2. Check the URL - it should contain your organization_id and site_id
3. Update the configuration file with these values

## Battery Models

This CLI has been tested with:
- **Enerezza Plus EGS-MC1650** (16.5 kWh)

If you have a different battery model, update the `capacity_kwh` value in your configuration file.

## Requirements

- Python 3.10 or higher
- Internet connection to access Kyocera solar portal

## How It Works

The CLI authenticates with the Kyocera solar portal and fetches real-time data about your system. It displays:

- **Solar**: Current power generation (🔆 day, 🌙 night)
- **Grid**: Import (negative) or export (positive) to the grid
- **Battery**: Charge/discharge rate with time estimates
- **Home**: Current consumption with clean energy percentage

The clean energy percentage shows what portion of your home consumption is coming from solar + battery (vs. grid).

## Author

Made by [Jordy Meow](https://jordymeow.com)

## License

MIT License - feel free to use and modify!

## Contributing

Found a bug or have a feature request? Please open an issue on GitHub!
