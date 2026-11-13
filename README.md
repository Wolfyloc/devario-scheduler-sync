# Devario Scheduler Sync

A CoolerControl integration plugin for Devario Linux that automatically switches cooling Modes based on the active sched-ext CPU scheduler.

## Overview

Devario Scheduler Sync monitors the active CPU scheduler using `scxcli` and communicates with CoolerControl through its REST API.

When the scheduler changes, the plugin activates the CoolerControl Mode assigned to that scheduler. This allows users to configure different fan and pump behaviors for gaming, everyday workloads, or quieter operation.

The plugin works independently of the application used to change schedulers, including Pearl and terminal-based tools.

## Features

- **Automatic Mode Switching:** Applies cooling Modes when the active CPU scheduler changes.
- **Dynamic Scheduler Discovery:** Detects available schedulers without requiring hardcoded entries.
- **Customizable Mappings:** Assigns CoolerControl Modes to individual schedulers.
- **Background Operation:** Runs as a CoolerControl-managed systemd service.
- **Transition Handling:** Avoids applying cooling Modes during temporary scheduler transition states.
- **Persistent Configuration:** Preserves mappings between service restarts.

## Requirements

- Devario Linux
- CoolerControl v5 or higher
- `scxcli` and a compatible sched-ext scheduler setup
- Python 3 and the dependencies required by the plugin
- A CoolerControl API token with permission to change Modes

## Installation

1. Clone this repository:

   ```bash
   git clone https://github.com/Wolfyloc/devario-scheduler-sync.git
   cd devario-scheduler-sync
   ```

2. Run the installer:

   ```bash
   sudo ./install.sh
   ```

3. Follow the installer prompts to configure the CoolerControl API token if one has not already been saved.

4. Open **CoolerControl → Plugins → devario_scheduler_sync** to configure scheduler-to-Mode mappings.

The installer registers the plugin with CoolerControl and restarts the CoolerControl service.

## Configuration

Use the plugin interface to assign a CoolerControl Mode to each discovered scheduler.

Example configuration:

| Scheduler | CoolerControl Mode |
|---|---|
| Cosmos | Testing |
| LAVD | Testing |
| Cake | Quiet |
| EEVDF | No change |
| Default kernel scheduler | No change |

These mappings are examples. Users can select whichever cooling Modes suit their systems.

Newly discovered schedulers default to **No change** until configured.

## Troubleshooting

Check the plugin service:

```bash
systemctl status cc-plugin-devario_scheduler_sync.service
```

View recent logs:

```bash
journalctl -u cc-plugin-devario_scheduler_sync.service -n 50 --no-pager
```

If the plugin does not appear in CoolerControl, check that the installation completed successfully and restart CoolerControl.

## Security

The plugin uses a dedicated unprivileged system account and an API token to communicate with CoolerControl.

Never commit or publish your API token, private credentials, or machine-specific secrets.

## Version

**v0.2** — Dynamic scheduler discovery, configurable cooling Mode mappings, and improved scheduler transition handling.