# Soundboard Snag

A Python tool to download audio files from soundboard.com with clean, normalized filenames.

## Features

- 🎯 **Smart Search**: Search for soundboards with quality filters
- 📥 **Bulk Download**: Download all sounds from a board or search results
- 🏷️ **Clean Filenames**: Automatically sanitizes and normalizes filenames
- 🔍 **Quality Filters**: Filter by views and sound count (default: 10 views, 3 sounds minimum)
- 🚀 **Fast & Efficient**: Dynamic pagination, early exit on failures
- 🎨 **Colored Output**: Beautiful terminal output with progress tracking
- 🛡️ **Smart Detection**: Automatically detects play-only boards
- 💻 **Cross-Platform**: Works on Windows, macOS, and Linux
- 📦 **Zero Dependencies**: Uses only Python standard library

## Requirements

- Python 3.6 or higher
- No external dependencies required!

## Installation

1. Clone this repository:
```bash
git clone https://github.com/yourusername/soundboard-snag.git
cd soundboard-snag
```

2. Make the script executable (optional):
```bash
chmod +x soundboard-snag.py
```

## Usage

### Search for Boards (Recommended First Step)

Search automatically filters low-quality boards by default:

```bash
# Basic search
python3 soundboard-snag.py --search "star wars"

# Search with custom quality filters
python3 soundboard-snag.py --search "hockey" --min-views 100 --min-sounds 10

# Search without filters (show all results)
python3 soundboard-snag.py --search "test" --min-views 0 --min-sounds 0

# Search more boards
python3 soundboard-snag.py --search "minecraft" --max 50
```

### Download by Board Name

After searching, download using the board name:

```bash
python3 soundboard-snag.py --board starwars
```

### Search and Download All Results

Search and automatically download all matching boards:

```bash
python3 soundboard-snag.py --search-and-download "hockey" --max 10
```

### Download by URL

Download directly using a soundboard URL:

```bash
python3 soundboard-snag.py --url https://www.soundboard.com/sb/starwars
```

### Interactive Mode

Run without arguments for interactive mode:

```bash
python3 soundboard-snag.py
```

## Command-Line Options

| Option | Description |
|--------|-------------|
| `-s, --search` | Search for downloadable boards |
| `--search-and-download` | Search and download all results automatically |
| `-b, --board` | Download by board name |
| `-u, --url` | Download by full URL |
| `--max` | Maximum boards to check in search (default: 20) |
| `--min-views` | Minimum views required (default: 10, use 0 for no filter) |
| `--min-sounds` | Minimum sounds required (default: 3, use 0 for no filter) |
| `--debug` | Show all boards analyzed, including filtered ones |

## Examples

```bash
# Search for popular Star Wars boards
python3 soundboard-snag.py --search "star wars" --min-views 1000

# Download a specific board
python3 soundboard-snag.py --board R2D2_R2_D2_sounds

# Search and download hockey boards
python3 soundboard-snag.py --search-and-download "hockey" --max 5

# Debug mode to see filtering details
python3 soundboard-snag.py --search "test" --debug
```

## Features in Detail

### Quality Filters

By default, search results are filtered to show only quality boards:
- **Minimum 10 views**: Filters out brand new/untested boards
- **Minimum 3 sounds**: Filters out incomplete/test boards

Use `--min-views 0 --min-sounds 0` to disable filtering.

### Smart Filename Handling

- Decodes HTML entities (e.g., `&#039;` → `'`)
- Removes UUID patterns from filenames
- Normalizes spacing and punctuation
- Handles Windows reserved names (CON, PRN, AUX, etc.)
- Applies title case to all-lowercase or all-uppercase names
- Cross-platform compatible sanitization

### Download Protection

- **Play-Only Detection**: Automatically detects boards with downloads disabled
- **Consecutive Failure Exit**: Exits after 2 consecutive failures to avoid wasting resources
- **Empty Directory Cleanup**: Removes empty directories if download fails immediately
- **Rate Limiting**: Respects server with 0.5s delay between requests

### Progress Tracking

- Shows current progress: `[3/20]`
- Displays file sizes: `(125.4 KB)`
- Color-coded status: ✓ Success, ○ Skipped, ✗ Failed
- Summary statistics at the end

## Output

Files are saved to a subfolder named after the board in your current directory:

```
./
├── starwars/
│   ├── R2-D2 Scream.mp3
│   ├── Chewbacca Roar.mp3
│   └── Lightsaber Sound.mp3
└── hockey/
    ├── Goal Horn.mp3
    └── Skate Sound.mp3
```

## Limitations

- Only works with boards that have **download buttons enabled** by the owner
- Boards in **play-only mode** cannot be downloaded (audio is access-controlled)
- The script will detect and warn about restricted boards before attempting downloads

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT License - see LICENSE file for details

## Disclaimer

This tool is for personal use only. Please respect copyright and the terms of service of soundboard.com. Only download content you have the right to access.
