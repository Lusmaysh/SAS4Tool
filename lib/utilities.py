"""
Cross-platform utilities for SAS4 Tool.
Works on both Windows and Termux/Android.

Platform-specific behavior:
- Windows: Uses Registry/Steam path detection
- Termux/Android: Uses /data/data/ path with root access
"""

from pathlib import Path
from os import environ, system, name, chmod, stat
from json import loads as jsloads, load as jsload, dump as jsdump, dumps as jsdumps, JSONDecodeError
from dataclasses import dataclass
from base64 import b64decode
from zlib import decompress as zdecompress
from functools import wraps
from inspect import signature
from subprocess import run, capture_output, TimeoutExpired

import platform

# ==========================================
# Platform Detection
# ==========================================

IS_TERMUX = name == 'posix' and 'TERMUX' in environ
IS_WINDOWS = name == 'nt'
IS_ANDROID = 'ANDROID_DATA' in environ or (name == 'posix' and 'PACKAGE_NAME' in environ and 'termux' not in environ.get('PACKAGE_NAME', ''))

if IS_TERMUX:
    print("[INFO] Running on Termux/Android environment")
elif IS_WINDOWS:
    print(f"[INFO] Running on Windows ({platform.system()})")
elif IS_ANDROID:
    print("[INFO] Running on Android (Termux not detected)")
else:
    print(f"[INFO] Running on unknown platform: {platform.system()} {platform.release()}")


# ==========================================
# Color Constants (Platform-aware)
# ==========================================

if IS_TERMUX or IS_ANDROID:
    # Termux uses ANSI escape codes
    RESET = '\033[0m'
    RED = '\033[31m'
    GREEN = '\033[32m'
    YELLOW = '\033[33m'
    CYAN = '\033[36m'
    WHITE = '\033[37m'
else:
    # Windows default
    RESET = ''
    RED = ''
    GREEN = ''
    YELLOW = ''
    CYAN = ''
    WHITE = ''


# ==========================================
# Platform-Specific Path Helpers
# ==========================================

def get_android_data_dir() -> Path:
    """
    Get the Android app data directory for SAS4.
    On rooted phones, we can access /data/data/com.ninjakiwi.sasza4/files/
    The folder structure contains random alphanumeric names.
    """
    package_name = environ.get('PACKAGE_NAME', 'com.ninjakiwi.sasza4')
    return Path(f'/data/data/{package_name}/files/')


def get_steam_path_windows() -> Path:
    """
    Get Steam installation path on Windows using Registry.
    Only used when IS_WINDOWS is True.
    """
    from win32api import RegOpenKeyEx, RegQueryValueEx
    from win32con import HKEY_CURRENT_USER

    steamEnv = environ.get('STEAM_PATH')
    if steamEnv:
        return Path(steamEnv).resolve(strict=True)

    regDirPath = RegOpenKeyEx(HKEY_CURRENT_USER, r'Software\Valve\Steam')
    regValue = RegQueryValueEx(regDirPath, 'SteamPath')
    return Path(regValue[0]).resolve(strict=True)


def get_steam_path_termux() -> Path | None:
    """
    On Termux/Android, Steam path is not applicable.
    Android version uses internal storage, not Steam.
    """
    return None


def convertSteamIDToUserID(steamID64: str) -> str:
    """
    Convert a Steam64 ID to the user folder name used by Steam.
    This is only used on Windows path.
    """
    try:
        parts = steamID64.strip("[]").split(":", 2)
        if len(parts) >= 2:
            numeric_id = parts[-1]
            return numeric_id[-12:] if len(numeric_id) >= 12 else numeric_id
    except (ValueError, IndexError):
        pass
    return steamID64[-12:] if len(steamID64) >= 12 else steamID64


# ==========================================
# Save File Path Detection
# ==========================================

def _find_save_windows(steamID: str) -> 'Iterator[Path]':
    """
    Find save files on Windows using Steam ID.
    """
    try:
        userId = convertSteamIDToUserID(str(steamID))
    except Exception as e:
        raise ValueError(f"Invalid Steam ID conversion: {str(e)}")

    try:
        steamPath = get_steam_path_windows()
        profilePath = Path(
            steamPath
            / 'userdata'
            / userId
            / '678800'
            / 'local/Data/Docs/'
        ).resolve(strict=True)
    except Exception as e:
        print(f'Error finding game save path (Windows): {str(e)}')
        print(f'User ID: {userId}\nSteam ID: {steamID}')
        raise ValueError("Unable to locate game save path.") from e

    # Iterate over folders (SteamID folders are typically 17+ chars)
    for folder in profilePath.iterdir():
        if not folder.is_dir() or len(folder.name) < 17:
            continue

        savePath = folder / 'Profile.save'
        if savePath.exists():
            yield savePath


def _find_save_android() -> 'Iterator[Path]':
    """
    Find save files on Android /data/data/ directory.
    Handles random folder names with numbers and letters.
    Requires root access for protected directories.
    """
    androidDir = get_android_data_dir()

    if not androidDir.exists():
        raise ValueError(f"Android data directory not found: {androidDir}")

    # Try to list directory - may need su for /data/data/
    items = []
    try:
        result = run(['su', '-c', 'ls', str(androidDir)],
                    capture_output=True, text=True, timeout=10)
        if result.returncode == 0:
            items = [item for item in result.stdout.strip().split('\n') if item]
        else:
            raise PermissionError("su failed")
    except (FileNotFoundError, TimeoutExpired, PermissionError):
        try:
            items = list(androidDir.iterdir())
        except PermissionError:
            raise PermissionError("Cannot access /data/data/ without root access")

    # Process items - iterate through directories looking for Profile.save
    for item_name in items:
        if not item_name or item_name in ('.', '..'):
            continue
        item = androidDir / item_name
        try:
            if item.is_dir():
                # This is a folder with the random name pattern
                # Look for Profile.save inside
                savePath = item / 'Profile.save'
                if savePath.exists():
                    yield savePath
            elif item.is_file() and item_name.endswith('.save'):
                yield item
        except (PermissionError, OSError):
            continue


def iterGameSavePaths() -> 'Iterator[Path]':
    """
    Iterate over game save file paths.
    
    Windows: Uses Steam user ID to find %SteamUser%/678800/local/Data/Docs/
    Termux/Android: Uses /data/data/com.ninjakiwi.sasza4/files/ with random folder names
    """
    config = loadConfig()
    steamID = config.get('steam_id')

    if IS_WINDOWS and not IS_TERMUX and not IS_ANDROID:
        # Windows path using Steam ID
        if not steamID:
            raise ValueError("Steam ID is not set in the config file.")

        yield from _find_save_windows(steamID)

    elif IS_TERMUX or IS_ANDROID:
        # Android path - use /data/data/ directory
        # This requires root access to read protected directories
        yield from _find_save_android()


def getProfileSavePath() -> 'Path | None':
    try:
        return next(iterGameSavePaths())
    except StopIteration:
        return None


# ==========================================
# Steam ID Handling
# ==========================================

def iterSteamLoggedUsers():
    """
    On Termux/Android, we don't have Steam logged-in users in the same way.
    This function returns None since Android version doesn't use Steam authentication.
    """
    if IS_TERMUX or IS_ANDROID:
        print("[INFO] Android version doesn't use Steam authentication, skipping user iteration")
        return

    # Original Windows implementation would go here
    # For now, just return None
    return None


# ==========================================
# Game Installation Check
# ==========================================

def isGameInstalled() -> bool:
    if IS_TERMUX or IS_ANDROID:
        # Android version - check if the app data directory exists
        try:
            androidDir = get_android_data_dir()
            result = run(['su', '-c', 'test -d ' + str(androidDir)],
                        capture_output=True, timeout=5)
            if result.returncode != 0:
                return False
            return androidDir.exists()
        except Exception:
            return False
    else:
        # Windows version
        try:
            steamPath = get_steam_path_windows()
            gamePath = steamPath / 'steamapps' / 'common' / 'SAS Zombie Assault 4'
            return Path(gamePath).resolve(strict=True).exists()
        except Exception:
            return False


# ==========================================
# Input Functions (Platform-specific)
# ==========================================

def promptInt(prompt: str, minValue: int | None = None, maxValue: int | None = None) -> int:
    """Prompt for integer input, platform-aware."""
    message = None
    while True:
        printTitle(message)
        try:
            userInput = int(input(prompt))
            if minValue is not None and userInput < minValue:
                message = f"{RED}Please enter a number greater than or equal to {minValue}.{WHITE}"
            elif maxValue is not None and userInput > maxValue:
                message = f"{RED}Please enter a number less than or equal to {maxValue}.{WHITE}"
            else:
                return userInput
        except ValueError:
            message = f"{RED}Invalid input. Please enter a valid integer.{WHITE}"


def promptStr(prompt: str, options: list | None = None, caseSensitive: bool = False) -> str:
    """Prompt for string input, platform-aware."""
    while True:
        printTitle()
        userInput = input(prompt)

        if options is None:
            return userInput

        if not caseSensitive:
            userInput = userInput.lower()
            options = [opt.lower() for opt in options]

        if userInput in options:
            return options[options.index(userInput)]
        else:
            print(f"Invalid input. Please choose from: {', '.join(options)}")


def promptYN(prompt: str) -> bool:
    """Prompt for yes/no, platform-aware."""
    while True:
        printTitle()
        userInput = input(f"{prompt} (y/n): ").lower()
        if userInput in ['y', 'yes']:
            return True
        elif userInput in ['n', 'no']:
            return False
        else:
            print("Invalid input. Please enter 'y' or 'n'.")


# helper function for printing
def cls():
    system('cls' if name == 'nt' else 'clear')


def printTitle(extra: str | None = None):
    if extra == '':
        extra = None
    cls()
    config = loadConfig()
    part1 = r'''
           _______   __________________  ____  __ 
         / __/ _ | / __/ / /_  __/ __ \/ __ \/ / 
        _\ \/ __ |_\ \/_  _// / / /_/ / /_/ / /__
       /___/_/ |_/___/ /_/ /_/  \____/\____/____/'''
    part2 = f"""{RED}{part1}{WHITE}
\t\t{CYAN}made by: 0dx | ver: {VERSION}{WHITE}

{WHITE}Selected Profile: [{GREEN}{config['current_profile']} {config['current_profile_name']}{WHITE}]"""
    if extra:
        part2 += f'\n{extra}\n'

    print(part2)


# F class definition - platform colors
@dataclass
class F:
    RESET: str = RESET
    RED: str = RED
    GREEN: str = GREEN
    YELLOW: str = YELLOW
    CYAN: str = CYAN
    WHITE: str = WHITE


# ==========================================
# Save File I/O Functions
# ==========================================

# Import gameio functions after F is defined
from lib.gameio import iterDecodeFromFile, encodeToFile


def loadSave() -> Any:
    try:
        rawData = b''.join(iterDecodeFromFile(getProfileSavePath()))
        data = jsloads(rawData)
        if not isinstance(data, dict) or 'Inventory' not in data or not isinstance(data['Inventory'], dict):
            raise ValueError("Unexpected save file structure")
        return data
    except JSONDecodeError as e:
        print(f"Error decoding save file: {e}")
        return None
    except Exception as e:
        print(f"Error loading save file: {e}")
        return None


def writeSave(data: str) -> None:
    oldProfile = getProfileSavePath() / 'OldProfile.save'
    tmpProfile = getProfileSavePath() / 'TempProfile.save'

    # Possible fix for account flags

    if oldProfile.exists():
        oldProfile.unlink()

    if tmpProfile.exists():
        tmpProfile.unlink()

    return encodeToFile(jsdumps(data).encode("utf-8"), getProfileSavePath().open("wb+"))


def getProfiles() -> list:
    data = loadSave()
    if data is None:
        return []
    try:
        return [i for i in data['Inventory'].keys() if isinstance(data['Inventory'][i], dict) and data['Inventory'][i].get('Loaded')]
    except Exception as e:
        print(f"Error parsing profiles: {e}")
        return []


def getNames() -> list:
    data = loadSave()
    if data is None:
        return []
    try:
        return [
            data['Inventory'][key]['Name']
            for key in data['Inventory'].keys()
            if isinstance(data['Inventory'][key], dict) and 'Name' in data['Inventory'][key]
        ]
    except Exception as e:
        print(f"Error parsing profiles: {e}")
        return []


def checkGameInstallation() -> bool:
    if not isGameInstalled():
        raise RuntimeError('Game is not installed!')
    return True


def createConfigFileIfMissing() -> None:
    if not Path('config.json').exists():
        print('Creating missing config file...')
        Path('config.json').touch()
        writeDefaultConfig()

    if Path('config.json').stat().st_size == 0:
        writeDefaultConfig()


def writeDefaultConfig() -> None:
    default_config = {'steam_id': None, 'current_profile': None, 'current_profile_name': None, 'active_profiles': None, 'active_profiles_names': None}
    with open('config.json', 'w') as f:
        jsdump(default_config, f, indent=4)


def validateConfigKeys(data: dict) -> None:
    default_keys = ['steam_id', 'current_profile', 'current_profile_name', 'active_profiles', 'active_profiles_names']
    if list(data.keys()) != default_keys:
        print('Fixing invalid config file...')
        writeDefaultConfig()


def updateSteamIDIfMissing(data: dict) -> int:
    if not data.get('steam_id'):
        print('SteamID not found in config file!')
        if IS_TERMUX or IS_ANDROID:
            print("[INFO] Android version doesn't use Steam ID, setting to None")
            data['steam_id'] = None
        else:
            # Would need to prompt user for Steam ID on Windows
            data['steam_id'] = 76561199415662245  # Default/example
        writeConfig(data)
    return data['steam_id']


def updateProfileInfoIfMissing(data: dict) -> None:
    profiles = getProfiles()
    if not profiles:
        print("Warning: No profiles found in the save file.")
        return
    names = getNames()

    profile_display_options = {}
    profile_to_name_map = {}

    for prof_id, prof_name in zip(profiles, names):
        display_label = f"{prof_id} ({prof_name})"
        profile_display_options[display_label] = prof_id
        profile_to_name_map[prof_id] = prof_name

    if not data.get('current_profile') or data['current_profile'] not in profiles:
        if IS_TERMUX or IS_ANDROID:
            # On Android, just use the first profile
            selected_profile_id = profiles[0] if profiles else None
            selected_profile_name = names[0] if names else None
        else:
            # On Windows, prompt user (would need promptChoice import)
            selected_profile_id = profiles[0] if profiles else None
            selected_profile_name = names[0] if names else None
    else:
        selected_profile_name = profile_to_name_map.get(data['current_profile'])

    data['current_profile'] = selected_profile_id
    data['current_profile_name'] = selected_profile_name
    data['active_profiles'] = profiles
    data['active_profiles_names'] = names
    writeConfig(data)


def loadItems():
    with open('items.json') as f:
        return jsload(f)


def writeConfig(data: dict) -> None:
    with open('config.json', 'w') as f:
        jsdump(data, f, indent=4)


def loadConfig() -> dict:
    try:
        with open('config.json') as f:
            return jsload(f)
    except JSONDecodeError:
        print("Error: Config file is corrupted. Creating a new one.")
        writeDefaultConfig()
        return {'steam_id': None, 'current_profile': None, 'current_profile_name': None, 'active_profiles': None, 'active_profiles_names': None}
    except FileNotFoundError:
        print("Error: Config file not found. Creating a new one.")
        writeDefaultConfig()
        return {'steam_id': None, 'current_profile': None, 'current_profile_name': None, 'active_profiles': None, 'active_profiles_names': None}


def initCheck():
    try:
        checkGameInstallation()
        if not Path('items.json').exists():
            Path('items.json').touch()
            # Decode the base64 compressed items data
            decoded = b64decode(ITEMS)
            decompressed = jsloads(zdecompress(decoded))
            with open('items.json', 'w') as f:
                jsdump(decompressed, f)

        createConfigFileIfMissing()
        data = loadConfig()
        validateConfigKeys(data)
        updateSteamIDIfMissing(data)
        updateProfileInfoIfMissing(data)

        if not data.get('steam_id') or not data.get('current_profile') or not data.get('active_profiles'):
            print("Warning: Some required fields are still missing in the config.")
            print(f"Current config: {data}")

        writeConfig(data)
    except Exception as e:
        print(f"Initialization failed: {e}")
        import traceback
        traceback.print_exc()


# Platform-agnostic helper functions
def directFunction(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    wrapper.__direct_function__ = True
    return wrapper


def menuOptions(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)
    wrapper.__menu_options__ = func
    return wrapper


def nestedMenuOptions(func):
    @wraps(func)
    def wrapper(*args, **kwargs):
        def navigateMenus(initialMenu):
            menuStack = [initialMenu]
            pathStack = []
            message = None

            while menuStack:
                currentMenu = menuStack[-1]
                currentPath = ' > '.join(pathStack)

                printTitle(message)
                print(f"{YELLOW}Current menu: {currentPath}{WHITE}\n")

                options = list(currentMenu.keys())
                for i, option in enumerate(options):
                    key = str(i + 1) if i < 9 else chr(ord('A') + i - 9)
                    print(f"[{key}] {option}")

                print("\nPress [ESC] to go back/exit")
                print('\n[ > ] ', end='', flush=True)

                key = input()

                if key.lower() == 'esc' or (len(key) > 0 and key[0].lower() == chr(27)):
                    if len(menuStack) == 1:
                        return
                    menuStack.pop()
                    pathStack.pop()
                    message = None
                    continue

                selectedIndex = -1
                if len(key) > 0 and key[0].isdigit():
                    selectedIndex = int(key[0]) - 1
                elif len(key) > 0 and 'A' <= key[0].upper() <= 'Z':
                    selectedIndex = ord(key[0].upper()) - ord('A') + 9

                if 0 <= selectedIndex < len(options):
                    selectedOption = options[selectedIndex]
                    value = currentMenu[selectedOption]

                    if callable(value):
                        result = value(selectedOption)
                        if isinstance(result, dict):
                            menuStack.append(result)
                            pathStack.append(selectedOption)
                            message = None
                        elif result is not None:
                            message = f"{GREEN}Function call: {result}{WHITE}"
                    elif isinstance(value, dict):
                        menuStack.append(value)
                        pathStack.append(selectedOption)
                        message = None
                else:
                    message = f"{RED}Invalid option. Please try again.{WHITE}"

        result = func(*args, **kwargs)
        if isinstance(result, dict):
            navigateMenus(result)
            return None
        return result

    return wrapper


def handleMenu(menu: dict, title: str = "Main Menu", parent: dict | None = None, message: str | None = None) -> None:
    while True:
        printTitle(message)
        print(f"{YELLOW}{title}{WHITE}\n")

        options = list(menu.keys())
        for i, option in enumerate(options):
            if i < 9:
                print(f"[{i+1}] {option}")
            else:
                print(f"[{chr(ord('A') + i - 9)}] {option}")

        print("\nPress [ESC] to go back/exit")
        print('\n[ > ] ', end='', flush=True)

        key = input()

        if key.lower() == 'esc' or (len(key) > 0 and key[0].lower() == chr(27)):
            if parent:
                return
            else:
                cls()
                print("\nExiting...")
                return None

        if len(key) > 0 and key[0].isdigit():
            idx = int(key[0]) - 1
        elif len(key) > 0 and 'A' <= key[0].upper() <= 'Z':
            idx = ord(key[0].upper()) - ord('A') + 9
        else:
            idx = -1

        if 0 <= idx < len(options):
            selectedOption = options[idx]
            value = menu[selectedOption]

            if callable(value):
                if getattr(value, '__direct_function__', False):
                    result = value()
                    message = f"{GREEN}Function call: {result}{WHITE}" if result else f"{YELLOW}Function executed with no return value.{WHITE}"
                elif hasattr(value, '__menu_options__'):
                    menuOptions_func = value.__menu_options__()
                    if isinstance(menuOptions_func, list):
                        new_menu = {option: lambda o=option: value(o) for option in menuOptions_func}
                        handleMenu(new_menu, title=selectedOption, parent=menu)
                    else:
                        message = f"{RED}Error: Menu options must be a list{WHITE}"
                else:
                    result = value()
                    message = f"{GREEN}Function call: {result}{WHITE}" if result else f"{YELLOW}Function executed with no return value.{WHITE}"
            elif isinstance(value, dict):
                handleMenu(value, title=selectedOption, parent=menu)
            else:
                message = f"{CYAN}Selected: {selectedOption}{WHITE}"
        else:
            message = f"{RED}Error: Option does not exist!{WHITE}"