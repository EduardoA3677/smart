import subprocess
import tempfile
import os
import sys
import json
import csv
import atexit
import re
import argparse
import time
import datetime
import hashlib
from colorama import init, Fore, Style
from collections import OrderedDict

init(autoreset=True)

HISTORY_FILE = ".smart_cherry_pick_history"
COMMIT_DEP_CACHE = ".smart_cherry_pick_dependency_cache.json"
AUTHOR_MAP_CACHE = ".smart_cherry_pick_author_map.json"
COMMITS_LIST_FILE = ".smart_cherry_pick_commits.json"
LOG_FILE = ".smart_cherry_pick_log.txt"
CONFIG_FILE = ".smart_cherry_pick_config.json"
STATS_FILE = ".smart_cherry_pick_stats.csv"
TEMP_FILES = [COMMIT_DEP_CACHE]

cherry_pick_queue = []
final_commits = []
analyzed_commits = set()
applied_commits = set()
file_renames = {}
stop_analysis = False
initial_commit = None
initial_commits = []
author_map = {}
skipped_commits = set()
remote_name = None
auto_mode = False
verbose_mode = False
dry_run = False
start_time = time.time()
processed_missing_files = set()  
created_files = set()  
stats_data = {}

config = {
    "max_commits_display": 5,
    "max_search_depth": 100,
    "rename_detection_threshold": 50,  
    "default_editor": None,  
    "auto_add_dependencies": False,
    "show_progress_bar": True,
    "max_retries": 3,
    "retry_delay": 2,
    "record_stats": True,
}

def clean_temp_files():
    for f in TEMP_FILES:
        if os.path.exists(f):
            try:
                os.remove(f)
            except Exception:
                pass

clean_temp_files()
atexit.register(clean_temp_files)

def log_message(message, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG_FILE, "a") as f:
        f.write(f"{timestamp} [{level}] {message}\n")

    if verbose_mode:
        level_colors = {
            "INFO": Fore.BLUE,
            "WARNING": Fore.YELLOW,
            "ERROR": Fore.RED,
            "SUCCESS": Fore.GREEN,
            "DEBUG": Fore.MAGENTA
        }
        color = level_colors.get(level, "")
        print(f"{color}[{level}] {message}")

def init_stats_file():
    if not config["record_stats"]:
        return
    
    headers = [
        "timestamp", 
        "commit", 
        "operation", 
        "status", 
        "duration_ms", 
        "file_count",
        "conflict_count", 
        "resolution_method", 
        "remote",
        "auto_mode"
    ]
    
    if not os.path.exists(STATS_FILE):
        with open(STATS_FILE, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)

def record_stats(commit, operation, status, duration_ms=0, file_count=0, 
                conflict_count=0, resolution_method="", remote="", auto_mode=False):
    if not config["record_stats"]:
        return
    
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    with open(STATS_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            timestamp, 
            commit, 
            operation, 
            status, 
            duration_ms,
            file_count, 
            conflict_count, 
            resolution_method,
            remote, 
            "true" if auto_mode else "false"
        ])

def start_operation_timer(commit, operation):
    global stats_data
    key = f"{commit}:{operation}"
    stats_data[key] = {
        "start_time": time.time(),
        "file_count": 0,
        "conflict_count": 0,
        "resolution_method": ""
    }
    return key

def end_operation_timer(key, status, resolution_method=""):
    global stats_data
    if key not in stats_data:
        return
    
    data = stats_data[key]
    duration_ms = int((time.time() - data["start_time"]) * 1000)
    commit, operation = key.split(":", 1)
    
    # Actualizar con información de resolución si se proporcionó
    if resolution_method:
        data["resolution_method"] = resolution_method
    
    record_stats(
        commit=commit,
        operation=operation,
        status=status,
        duration_ms=duration_ms,
        file_count=data["file_count"],
        conflict_count=data["conflict_count"],
        resolution_method=data["resolution_method"],
        remote=remote_name or "",
        auto_mode=auto_mode
    )
    
    del stats_data[key]

def load_config():
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded_config = json.load(f)
                config.update(loaded_config)
        except Exception as e:
            log_message(f"Error al cargar configuración: {str(e)}", "ERROR")

def save_config():
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def run(cmd, capture_output=True, input_text=None, retry=0, allow_fail=False):

    if remote_name and f"{remote_name}/" in cmd:
        if ("git log" in cmd and ".." in cmd) or ("git show" in cmd and ":" in cmd):
            try:
                if verbose_mode:
                    log_message(f"Ejecutando: {cmd}", "DEBUG")
                result = subprocess.run(cmd, shell=True, capture_output=capture_output, text=True, input=input_text)
                if result.returncode == 0:
                    return result.stdout.strip() if capture_output else None
                local_cmd = cmd.replace(f"{remote_name}/", "")
                if verbose_mode:
                    log_message(f"Fallo remoto, intento local: {local_cmd}", "DEBUG")
                return run(local_cmd, capture_output, input_text, retry, allow_fail)
            except Exception:
                local_cmd = cmd.replace(f"{remote_name}/", "")
                return run(local_cmd, capture_output, input_text, retry, allow_fail)
    try:
        if verbose_mode:
            log_message(f"Ejecutando: {cmd}", "DEBUG")
        result = subprocess.run(cmd, shell=True, capture_output=capture_output, text=True, input=input_text)
        if result.returncode != 0:
            if allow_fail:
                return ""
            elif retry < config["max_retries"]:
                log_message(f"Comando falló. Reintento {retry+1}/{config['max_retries']}: {cmd}", "WARNING")
                time.sleep(config["retry_delay"])
                return run(cmd, capture_output, input_text, retry + 1, allow_fail)
        return result.stdout.strip() if capture_output else None
    except Exception as e:
        if allow_fail:
            return ""
        log_message(f"Error ejecutando comando '{cmd}': {str(e)}", "ERROR")
        if retry < config["max_retries"]:
            log_message(f"Reintento {retry+1}/{config['max_retries']}", "WARNING")
            time.sleep(config["retry_delay"])
            return run(cmd, capture_output, input_text, retry + 1, allow_fail)
        return ""

def select_option(options, prompt="Selecciona una opción: "):
    if auto_mode:
        log_message(f"Modo automático: seleccionando opción por defecto '{options[0]}'", "INFO")
        return options[0]

    for i, option in enumerate(options, 1):
        print(f"{Fore.CYAN}{i}.{Style.RESET_ALL} {option}")
    idx = input(prompt)
    try:
        idx = int(idx)
        if idx < 1 or idx > len(options):
            print(Fore.RED + "Opción inválida. Se usará la opción 1.")
            return options[0]
        return options[idx - 1]
    except:
        print(Fore.RED + "Entrada inválida. Se usará la opción 1.")
        return options[0]

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_history(commits):
    with open(HISTORY_FILE, "w") as f:
        json.dump(sorted(list(commits)), f, indent=2)

def load_dep_cache():
    if os.path.exists(COMMIT_DEP_CACHE):
        with open(COMMIT_DEP_CACHE, "r") as f:
            return json.load(f)
    return {}

def save_dep_cache(cache):
    with open(COMMIT_DEP_CACHE, "w") as f:
        json.dump(cache, f, indent=2)

def load_author_map():
    if os.path.exists(AUTHOR_MAP_CACHE):
        try:
            with open(AUTHOR_MAP_CACHE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_author_map(map_data):
    with open(AUTHOR_MAP_CACHE, "w") as f:
        json.dump(map_data, f, indent=2)

def save_commits_list(commits):
    with open(COMMITS_LIST_FILE, "w") as f:
        json.dump(commits, f, indent=2)

def load_commits_list():
    if os.path.exists(COMMITS_LIST_FILE):
        with open(COMMITS_LIST_FILE, "r") as f:
            return json.load(f)
    return []

def extract_username_from_email(email):
    match = re.match(r'^([^@]+)@github\.com$', email)
    if match:
        return match.group(1)

    match = re.match(r'^([^@+]+)(?:\+[^@]+)?@users\.noreply\.github\.com$', email)
    if match:
        return match.group(1)

    match = re.match(r'^([^@]+)@', email)
    if match:
        username = match.group(1).lower()
        if '.' in username or '-' in username:
            return username

    parts = email.split('@')[0].split('.')
    if len(parts) > 0:
        return parts[0].lower()

    return None

def infer_github_username(author_name, author_email):
    global author_map
    if author_name in author_map:
        return author_map[author_name]

    special_cases = {
        "Sultan Alsawaf": "kerneltoast",
        "EduardoA3677": "Eduardo3677",
    }

    if author_name in special_cases:
        author_map[author_name] = special_cases[author_name]
        save_author_map(author_map)
        return special_cases[author_name]

    if author_email:
        username = extract_username_from_email(author_email)
        if username:
            author_map[author_name] = username
            save_author_map(author_map)
            return username

    names = author_name.strip().split()
    if len(names) > 0:
        if len(names) == 1 and not names[0].startswith("git"):
            username = names[0].lower()
            author_map[author_name] = username
            save_author_map(author_map)
            return username

    return author_name

def get_commit_files(commit):
    ref = commit
    if remote_name:
        remote_ref = f"{remote_name}/{commit}"
        exists = run(f"git rev-parse --verify {remote_ref}^{{commit}} 2>/dev/null", allow_fail=True)
        if exists:
            ref = remote_ref
    try:
        files = run(f"git diff-tree --no-commit-id --name-status -r {ref}").splitlines()
        result = []
        for line in files:
            if not line:
                continue
            parts = line.split()
            if parts[0].startswith('R') and len(parts) >= 3:
                result.append(parts[2])
            elif len(parts) >= 2:
                result.append(parts[1])
        return result
    except Exception as e:
        log_message(f"Error al obtener archivos del commit {commit}: {str(e)}", "ERROR")
        return run(f"git show --pretty='' --name-only {ref}").splitlines()

def get_last_commit_affecting_file(file_path):
    cmd = f"git log -n 1 --pretty=format:'%H' -- {file_path}"
    if remote_name:
        cmd = f"git log -n 1 --pretty=format:'%H' {remote_name} -- {file_path}"
    return run(cmd).strip("'")

def ask_to_search_file(file_path):
    global stop_analysis

    print(Fore.YELLOW + f"\nEl archivo '{file_path}' no existe en la rama actual.")
    options = [
        "Buscar el archivo en commits anteriores",
        "Ignorar archivo y continuar con el cherry-pick",
        "Aplicar directamente los commits agregados hasta ahora",
        "Cancelar operación"
    ]

    if auto_mode:
        log_message(f"Modo automático: buscando archivo '{file_path}' en commits anteriores", "INFO")
        return True

    choice = select_option(options, "¿Qué deseas hacer? ")
    if choice.startswith("Buscar"):
        return True
    elif choice.startswith("Ignorar"):
        return False
    elif choice.startswith("Aplicar"):

        if len(final_commits) > 0:
            log_message("Saltando análisis y aplicando commits directamente", "INFO")
            stop_analysis = True
            ask_to_proceed()  
            sys.exit(0)  
        else:
            log_message("No hay commits para aplicar todavía", "WARNING")
            return False
    else:
        print(Fore.RED + "Operación cancelada por el usuario.")
        sys.exit(1)

def search_file_in_remote(file_path):
    if not remote_name:
        return None

    print(Fore.CYAN + f"Buscando archivo '{file_path}' en remote '{remote_name}'...")

    try:

        main_branches = ["master", "main", "develop", "dev"]
        for branch in main_branches:
            ref = f"{remote_name}/{branch}"
            exists = run(f"git rev-parse --verify {ref} 2>/dev/null")
            if exists:
                result = run(f"git ls-tree -r {ref} --name-only | grep -F '{file_path}'")
                if result:
                    print(Fore.GREEN + f"Archivo encontrado en {ref}")
                    commit = run(f"git log -n 1 --pretty=format:'%H' {ref} -- {file_path}")
                    return commit

        remote_branches = run(f"git branch -r | grep {remote_name}/ | sed 's/{remote_name}\\///'").splitlines()
        for branch in remote_branches:
            if branch in main_branches:
                continue  

            ref = f"{remote_name}/{branch}"
            result = run(f"git ls-tree -r {ref} --name-only | grep -F '{file_path}'")
            if result:
                print(Fore.GREEN + f"Archivo encontrado en {ref}")
                commit = run(f"git log -n 1 --pretty=format:'%H' {ref} -- {file_path}")
                return commit
    except Exception as e:
        log_message(f"Error al buscar en remote: {str(e)}", "ERROR")

    return None

def find_commit_adding_file(file_path):
    if remote_name:
        run(f"git fetch {remote_name}")
        result = run(f"git log --diff-filter=A --format='%H' {remote_name} -- {file_path}")
        if result:
            commits = result.splitlines()
            if commits:
                return commits[-1]
        result = run(f"git log --full-history --format='%H' {remote_name} -- {file_path} | tail -1")
        if result:
            return result
        remote_refs = run(f"git for-each-ref --format='%(refname:short)' refs/remotes/{remote_name}").splitlines()
        for ref in remote_refs:
            result = run(f"git log --diff-filter=A --format='%H' {ref} -- {file_path}")
            if result:
                commits = result.splitlines()
                if commits:
                    return commits[-1]
    result = run(f"git log --diff-filter=A --format='%H' -- {file_path}")
    if result:
        commits = result.splitlines()
        if commits:
            return commits[-1]
    result = run(f"git log --full-history --format='%H' -- {file_path} | tail -1")
    if result:
        return result
    basename = os.path.basename(file_path)
    search_cmd = f"git rev-list --all"
    if remote_name:
        search_cmd += f" {remote_name}"
    result = run(f"{search_cmd} | xargs -I{{}} git grep -l '{basename}' {{}} | head -1")
    if result:
        file_commit = run(f"git log -n 1 --pretty=format:'%H' {result}")
        if file_commit:
            return file_commit
    similar_files = find_similar_files(file_path)
    for similar, score in similar_files:
        result = run(f"git log --diff-filter=A --format='%H' -- {similar} | tail -1")
        if result:
            return result
    if remote_name:
        return search_file_in_remote(file_path)
    return None

def find_similar_files(file_path):
    # Caché para evitar búsquedas repetidas
    global similar_files_cache
    if 'similar_files_cache' not in globals():
        similar_files_cache = {}
    
    # Si ya buscamos este archivo antes, retornamos resultados en caché
    if file_path in similar_files_cache:
        return similar_files_cache[file_path]

    basename = os.path.basename(file_path)
    dirname = os.path.dirname(file_path)

    all_files = run("git ls-files").splitlines()
    similar_files = []

    # Si el nombre de archivo tiene extensión, buscar también sin extensión
    filebase, ext = os.path.splitext(basename)
    if ext:
        # Buscar archivos similares que podrían tener otra extensión
        for repo_file in all_files:
            repo_filebase, repo_ext = os.path.splitext(os.path.basename(repo_file))
            if filebase == repo_filebase and ext != repo_ext:
                similar_files.append((repo_file, 90))  # Alta similitud para mismo nombre con extensión diferente
    
    # Buscar por similitud de nombres
    for repo_file in all_files:
        repo_basename = os.path.basename(repo_file)
        repo_dirname = os.path.dirname(repo_file)

        name_similarity = calculate_similarity(basename, repo_basename)

        if dirname == repo_dirname:
            name_similarity += 20  # Bonus por estar en el mismo directorio
        
        # Busca si están en directorios similares
        dir_similarity = calculate_similarity(dirname, repo_dirname)
        if dir_similarity > 70:  # Si los directorios son muy similares
            name_similarity += 10

        if name_similarity >= config["rename_detection_threshold"]:
            similar_files.append((repo_file, name_similarity))

    # Si no encontramos archivos similares, intentar búsqueda más exhaustiva
    if len(similar_files) == 0 and len(filebase) > 3:
        # Buscar archivos con nombres parciales (útil para renombres mayores)
        for repo_file in all_files:
            repo_basename = os.path.basename(repo_file)
            if len(filebase) > 5 and (filebase[:5] in repo_basename or filebase[-5:] in repo_basename):
                similar_files.append((repo_file, 60))  # Similitud media para coincidencias parciales
    
    # Ordenar por similaridad y limitar resultados
    similar_files.sort(key=lambda x: x[1], reverse=True)
    results = similar_files[:5]
    
    # Guardar en caché
    similar_files_cache[file_path] = results
    return results

def calculate_similarity(str1, str2):
    if not str1 or not str2:
        return 0

    m, n = len(str1), len(str2)
    if m < n:
        return calculate_similarity(str2, str1)

    if n == 0:
        return 0

    prev_row = range(n + 1)
    for i, c1 in enumerate(str1):
        curr_row = [i + 1]
        for j, c2 in enumerate(str2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row

    distance = prev_row[n]
    max_len = max(m, n)
    similarity = ((max_len - distance) / max_len) * 100
    return round(similarity)

def find_commit_history_chain(file_path, target_commit):
    remote_ref = f"{remote_name}/" if remote_name else ""
    creation_commit = find_commit_adding_file(file_path)
    if not creation_commit:
        return []
    try:
        target_ref = target_commit
        creation_ref = creation_commit
        if remote_name:
            remote_target = f"{remote_name}/{target_commit}"
            if run(f"git rev-parse --verify {remote_target}^{{commit}} 2>/dev/null", allow_fail=True):
                target_ref = remote_target
            remote_creation = f"{remote_name}/{creation_commit}"
            if run(f"git rev-parse --verify {remote_creation}^{{commit}} 2>/dev/null", allow_fail=True):
                creation_ref = remote_creation
        if target_commit:
            result = run(f"git log --format='%H' {creation_ref}~1..{target_ref} -- {file_path}", allow_fail=True)
            if result:
                commits = result.splitlines()
                if creation_commit not in commits:
                    commits = [creation_commit] + commits
                return commits
        all_commits = run(f"git log --format='%H' --follow {remote_ref} -- {file_path}", allow_fail=True).splitlines()
        if not all_commits:
            return [creation_commit]
        return all_commits
    except Exception:
        return [creation_commit]

def find_file_history(file_path):
    cmd = f"git log --name-status --follow --format='%H %cr: %s' -- {file_path}"
    if remote_name:
        cmd = f"git log --name-status --follow --format='%H %cr: %s' {remote_name} -- {file_path}"

    return run(cmd)

def get_blame_and_grep_dependencies(commit, file, dep_cache=None):
    # Usar caché para evitar análisis repetitivos
    cache_key = f"{commit}:{file}"
    if dep_cache is not None and cache_key in dep_cache:
        return dep_cache[cache_key]

    remote_ref = f"{remote_name}/" if remote_name else ""
    commit_ref = f"{remote_ref}{commit}" if remote_name else commit

    # Obtener el contenido del archivo
    try:
        file_content = run(f"git show {commit}:{file}")
    except Exception:
        return []

    if not file_content:
        return []

    lines = file_content.splitlines()
    
    # Detectar el tipo de archivo para análisis específico de lenguaje
    _, extension = os.path.splitext(file)
    extension = extension.lower()
    
    # Analizar el blame para identificar commits que modificaron el archivo
    try:
        blame_cmd = f"git blame --porcelain {file}"
        if remote_name:
            blame_cmd = f"git blame --porcelain {remote_name} {file}"

        blame_output = run(blame_cmd)
    except Exception:
        blame_output = ""

    blame_commits = set()
    if blame_output:
        for line in blame_output.splitlines():
            if line and len(line.split()) >= 1 and len(line.split()[0]) == 40:  # 40 caracteres = hash SHA-1
                blame_commits.add(line.split()[0])

    # Iniciar la lista de commits sospechosos con los del blame
    suspects = set(blame_commits)
    
    # Análisis específico por tipo de archivo/lenguaje
    language_patterns = {
        ".py": {
            "imports": [
                r'import\s+([a-zA-Z0-9_.]+)',
                r'from\s+([a-zA-Z0-9_.]+)\s+import',
            ],
            "functions": [
                r'def\s+([a-zA-Z0-9_]+)\s*\(',
                r'([a-zA-Z0-9_]+)\s*\('
            ]
        },
        ".js": {
            "imports": [
                r'import\s+.*\s+from\s+[\'"]([^\'"]+)[\'"]',
                r'require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)',
            ],
            "functions": [
                r'function\s+([a-zA-Z0-9_]+)\s*\(',
                r'const\s+([a-zA-Z0-9_]+)\s*=\s*\(',
                r'let\s+([a-zA-Z0-9_]+)\s*=\s*\(',
                r'var\s+([a-zA-Z0-9_]+)\s*=\s*\(',
                r'([a-zA-Z0-9_]+)\s*:\s*function'
            ]
        },
        ".c": {
            "imports": [
                r'#\s*include\s*[<"]([^>"]+)[>"]',
            ],
            "functions": [
                r'([a-zA-Z0-9_]+)\s*\(',
                r'typedef\s+struct\s+([a-zA-Z0-9_]+)'
            ]
        },
        ".cpp": {
            "imports": [
                r'#\s*include\s*[<"]([^>"]+)[>"]',
            ],
            "functions": [
                r'([a-zA-Z0-9_:]+)::[a-zA-Z0-9_]+\s*\(',
                r'([a-zA-Z0-9_]+)\s*\(',
                r'class\s+([a-zA-Z0-9_]+)'
            ]
        },
        ".h": {
            "imports": [
                r'#\s*include\s*[<"]([^>"]+)[>"]',
            ],
            "functions": [
                r'([a-zA-Z0-9_]+)\s*\(',
                r'typedef\s+struct\s+([a-zA-Z0-9_]+)',
                r'class\s+([a-zA-Z0-9_]+)'
            ]
        },
        ".java": {
            "imports": [
                r'import\s+([a-zA-Z0-9_.]+)',
            ],
            "functions": [
                r'([a-zA-Z0-9_]+)\s*\(',
                r'class\s+([a-zA-Z0-9_]+)',
                r'interface\s+([a-zA-Z0-9_]+)'
            ]
        }
    }
    
    # Si no hay un patrón específico para la extensión, usar uno genérico
    if extension not in language_patterns:
        patterns = {
            "functions": [r'([a-zA-Z0-9_]+)\s*\(']
        }
    else:
        patterns = language_patterns[extension]
    
    # Extraer funciones y símbolos importantes
    important_symbols = set()
    for i, line in enumerate(lines):
        line = line.strip()
        
        # Ignorar líneas vacías o comentarios
        if not line or line.startswith("//") or line.startswith("#"):
            continue
            
        # Buscar símbolos y funciones según el tipo de archivo
        for pattern_type, pattern_list in patterns.items():
            for pattern in pattern_list:
                matches = re.findall(pattern, line)
                for match in matches:
                    if isinstance(match, tuple):
                        match = match[0]  # En caso de que el regex devuelva grupos
                    if match and len(match) > 1:  # Ignore symbols that are too short
                        important_symbols.add(match)
    
    # Buscar commits asociados a los símbolos importantes
    for symbol in important_symbols:
        # Buscar sólo símbolos significativos
        if len(symbol) < 3 or not symbol.isidentifier():
            continue
            
        grep_cmd = f"git log -S'{symbol}' --pretty=format:'%H' -- {file}"
        if remote_name:
            grep_cmd = f"git log -S'{symbol}' --pretty=format:'%H' {remote_name} -- {file}"

        grep_result = run(grep_cmd)
        if grep_result:
            suspects.update(grep_result.splitlines())
    
    # Analizar dependencias de archivos incluidos
    includes = extract_includes(file_content)
    for include_file in includes:
        try:
            include_commit = find_commit_adding_file(include_file)
            if include_commit:
                suspects.add(include_commit)
        except Exception:
            pass

    # Verificar si hay rename_dependencies especiales (para refactors)
    rename_grep_cmd = f"git log --name-status --format='%H' -M -C -- {file}"
    if remote_name:
        rename_grep_cmd = f"git log --name-status --format='%H' -M -C {remote_name} -- {file}"
    
    rename_output = run(rename_grep_cmd)
    if rename_output:
        for line in rename_output.splitlines():
            if len(line) == 40:  # Hash SHA-1
                suspects.add(line)
    
    # Eliminar el commit actual de la lista de dependencias
    if commit in suspects:
        suspects.remove(commit)
        
    # Convertir a lista para almacenar en caché
    suspects_list = list(suspects)
    if dep_cache is not None:
        dep_cache[cache_key] = suspects_list
    return suspects_list

def extract_includes(file_content):
    includes = []

    patterns = [
        r'#\s*include\s*[<"]([^>"]+)[>"]',  
        r'import\s+[\'"]([^\'"]+)[\'"]',    
        r'from\s+[\'"]([^\'"]+)[\'"]',      
        r'require\s*\(\s*[\'"]([^\'"]+)[\'"]\s*\)',  
        r'@import\s+[\'"]([^\'"]+)[\'"]',   
        r'<link[^>]+href=[\'"]([^\'"]+)[\'"]',  
    ]

    for pattern in patterns:
        matches = re.findall(pattern, file_content)
        for match in matches:

            path = match.strip()
            if not path.endswith(('.h', '.c', '.cpp', '.hpp', '.py', '.js', '.css')):

                if '.' not in os.path.basename(path):
                    for ext in ['.h', '.py', '.js']:
                        potential_path = path + ext

                        if run(f"git ls-files | grep -F '{potential_path}'"):
                            path = potential_path
                            break

            includes.append(path)

    return includes

def get_commit_context(commit):
    ref = commit
    commit_found = False

    local_exists = run(f"git cat-file -e {commit}^{{commit}} 2>/dev/null", allow_fail=True)
    if local_exists:
        commit_found = True

    if not commit_found and remote_name:

        run(f"git fetch {remote_name}", allow_fail=False)

        remote_branches = run(f"git branch -r --contains {commit} 2>/dev/null | grep {remote_name}/", allow_fail=True)
        if remote_branches:
            commit_found = True
            if verbose_mode:
                log_message(f"Commit {commit} encontrado en remoto: {remote_branches.splitlines()[0].strip()}", "INFO")
        else:

            run(f"git fetch {remote_name} {commit}", allow_fail=True)

            commit_found = run(f"git cat-file -e {commit}^{{commit}} 2>/dev/null", allow_fail=True) != ""

    if not commit_found and verbose_mode:
        log_message(f"Commit {commit} no encontrado localmente ni en referencia directa de remote {remote_name}", "WARNING")

    commit_hash = run(f"git log -1 --pretty=format:'%h' {ref}", allow_fail=True)
    if not commit_hash:
        return f"{commit[:7]} (commit no encontrado)"

    commit_author = run(f"git log -1 --pretty=format:'%an' {ref}")
    commit_email = run(f"git log -1 --pretty=format:'%ae' {ref}")
    commit_subject = run(f"git log -1 --pretty=format:'%s' {ref}")
    commit_date = run(f"git log -1 --pretty=format:'%cd' --date=short {ref}")

    return f"{commit_hash} ({commit_date}) - {commit_author} - {commit_subject}"

def add_commit_once(commit):
    if commit not in final_commits:
        final_commits.append(commit)
    if commit not in cherry_pick_queue:
        cherry_pick_queue.append(commit)

def count_unique_pending_commits():
    unique_commits = set(final_commits + cherry_pick_queue)
    if initial_commit and initial_commit not in unique_commits:
        unique_commits.add(initial_commit)
    return len(unique_commits)

def show_progress(current, total, message="Procesando"):
    if not config["show_progress_bar"] or dry_run:
        return

    bar_length = 40
    progress = min(1.0, current / total if total > 0 else 1.0)
    filled_length = int(bar_length * progress)
    bar = '█' * filled_length + '░' * (bar_length - filled_length)
    percent = int(progress * 100)

    print(f"\r{message}: [{bar}] {percent}% ({current}/{total})", end='')
    if current == total:
        print()

def analyze_commit(commit, dep_cache):
    global stop_analysis, processed_missing_files, created_files

    if stop_analysis:
        return

    if commit in analyzed_commits or commit in applied_commits:
        log_message(f"El commit {commit} ya fue analizado o aplicado.", "INFO")
        return

    if commit not in final_commits:
        final_commits.append(commit)

    analyzed_commits.add(commit)
    print(Fore.GREEN + f"\nAnalizando commit {commit}...")

    files = get_commit_files(commit)
    total_files = len(files)

    for idx, file in enumerate(files):
        show_progress(idx + 1, total_files, f"Analizando archivos de {commit[:8]}")
        actual_file = file_renames.get(file, file)
        if actual_file in created_files:
            continue
        file_exists = run(f"git ls-files --error-unmatch {actual_file} 2>/dev/null || echo 'NOT_EXISTS'")
        if file_exists == "NOT_EXISTS":
            file_commit_key = f"{file}:{commit}"
            if file_commit_key in processed_missing_files:
                continue
            processed_missing_files.add(file_commit_key)
            if not ask_to_search_file(actual_file):
                continue
            adding_commit = find_commit_adding_file(file)
            if adding_commit:
                commit_chain = find_commit_history_chain(file, commit)
                if commit_chain and len(commit_chain) > 1:
                    option = select_option([
                        f"Agregar toda la cadena de {len(commit_chain)} commits relacionados con este archivo",
                        f"Agregar solo el commit que creó el archivo ({adding_commit[:8]})",
                        f"Continuar con el cherry-pick ({count_unique_pending_commits()})"
                    ])
                    if option.startswith("Agregar toda"):
                        for c in commit_chain:
                            add_commit_once(c)
                        created_files.add(actual_file)
                        if stop_analysis:
                            return
                    elif option.startswith("Agregar solo"):
                        add_commit_once(adding_commit)
                        created_files.add(actual_file)
                        if stop_analysis:
                            return
                    else:
                        stop_analysis = True
                        return
                else:
                    option = select_option([
                        f"Agregar commit que creó el archivo {adding_commit[:8]}",
                        f"Continuar con el cherry-pick ({count_unique_pending_commits()})"
                    ])
                    if option.startswith("Agregar"):
                        add_commit_once(adding_commit)
                        created_files.add(actual_file)
                        if stop_analysis:
                            return
                    elif option.startswith("Continuar"):
                        stop_analysis = True
                        return
            else:
                option = select_option([
                    f"Especificar un archivo local equivalente",
                    f"Continuar con el cherry-pick ({count_unique_pending_commits()})",
                    f"Cancelar análisis"
                ])
                if option.startswith("Especificar"):
                    local_name = input(f"Archivo local para '{actual_file}': ").strip()
                    file_renames[actual_file] = local_name
                    save_file_renames()
                elif option.startswith("Cancelar"):
                    stop_analysis = True
                    return
        last_commit = get_last_commit_affecting_file(actual_file)
        if last_commit and last_commit != commit and last_commit not in analyzed_commits and last_commit not in applied_commits:
            option = select_option([
                f"Agregar commit faltante a la lista de commits a aplicar {last_commit}",
                f"Continuar con el cherry-pick ({count_unique_pending_commits()})"
            ])
            if option.startswith("Agregar"):
                add_commit_once(last_commit)
                analyze_commit(last_commit, dep_cache)
                if stop_analysis:
                    return
            elif option.startswith("Continuar"):
                stop_analysis = True
                return
        if stop_analysis:
            return
        dependencies = get_blame_and_grep_dependencies(commit, actual_file, dep_cache)
        relevant_deps = [dep for dep in dependencies if dep not in analyzed_commits and dep not in applied_commits and dep != commit]
        for dep_commit in relevant_deps:
            if config["auto_add_dependencies"]:
                add_commit_once(dep_commit)
                continue
            choice = select_option([
                f"Agregar commit faltante a la lista de commits a aplicar {dep_commit}",
                f"Continuar con el cherry-pick ({count_unique_pending_commits()})"
            ])
            if choice.startswith("Agregar"):
                add_commit_once(dep_commit)
                analyze_commit(dep_commit, dep_cache)
                if stop_analysis:
                    return
            elif choice.startswith("Continuar"):
                stop_analysis = True
                return
            if stop_analysis:
                return

def save_file_renames():
    with open(".smart_cherry_pick_renames.json", "w") as f:
        json.dump(file_renames, f, indent=2)

def load_file_renames():
    if os.path.exists(".smart_cherry_pick_renames.json"):
        with open(".smart_cherry_pick_renames.json", "r") as f:
            return json.load(f)
    return {}

def process_commit(commit, dep_cache):
    print(Fore.GREEN + f"\nProcesando commit: {get_commit_context(commit)}")
    
    if auto_mode:
        log_message("Modo automático: analizando commit para buscar dependencias", "INFO")
        analyze_commit(commit, dep_cache)
        return

    options = [
        "Analizar commit para buscar dependencias",
        "Aplicar cherry-pick directamente sin análisis",
        "Editar el contenido y mensaje del commit antes de aplicarlo",
        "Omitir este commit"
    ]
    choice = select_option(options, "¿Cómo deseas procesar este commit? ")

    if choice.startswith("Analizar"):
        analyze_commit(commit, dep_cache)
    elif choice.startswith("Aplicar"):
        print(Fore.GREEN + f"\nAplicando cherry-pick directamente para {commit}...")
        
        if dry_run:
            print(Fore.YELLOW + f"[Modo simulación] Se aplicaría cherry-pick a {commit}")
            final_commits.append(commit)
            return

        op_key = start_operation_timer(commit, "direct_cherry_pick")
        
        # Preparar la referencia del commit
        commit_ref = commit
        if remote_name:
            remote_ref = f"{remote_name}/{commit}"
            test_exists = run(f"git rev-parse --verify {remote_ref}^{{commit}} 2>/dev/null", allow_fail=True)
            if test_exists:
                commit_ref = remote_ref
        
        # Intentar aplicar el cherry-pick
        start_time = time.time()
        result = subprocess.run(f"git cherry-pick --empty=drop {commit_ref}", shell=True)
        duration = time.time() - start_time
        
        # Manejar el resultado
        if result.returncode == 0:
            applied_commits.add(commit)
            final_commits.append(commit)
            print(Fore.GREEN + f"Commit {commit} aplicado exitosamente en {duration:.2f} segundos.")
            end_operation_timer(op_key, "success")
        else:
            log_message(f"Error al aplicar cherry-pick directo para {commit}", "WARNING")
            handle_cherry_pick_error(commit)
    elif choice.startswith("Editar"):
        edit_commit_before_applying(commit)
    else:  # Omitir commit
        skipped_commits.add(commit)
        log_message(f"Commit {commit} omitido por elección del usuario.", "INFO")

def edit_commit_before_applying(commit):
    commit_ref = commit
    if remote_name:
        remote_ref = f"{remote_name}/{commit}"
        test_exists = run(f"git rev-parse --verify {remote_ref}^{{commit}} 2>/dev/null", allow_fail=True)
        if test_exists:
            commit_ref = remote_ref

    print(Fore.YELLOW + f"\nIniciando edición del commit {commit}...")
    # Ejecuta el cherry-pick en modo no-commit para obtener los cambios en el índice.
    result = subprocess.run(f"git cherry-pick -n {commit_ref}", shell=True)
    if result.returncode != 0:
        log_message(f"No se pudieron extraer los cambios del commit {commit} para editar.", "ERROR")
        return

    editor_cmd = get_preferred_editor()

    # Se obtiene la lista de archivos modificados a partir del índice
    changed_files = run("git diff --cached --name-only")
    if changed_files:
        archivos = changed_files.splitlines()
        print(Fore.YELLOW + f"Abriendo el editor ({editor_cmd}) para editar los siguientes archivos:")
        for a in archivos:
            print(Fore.CYAN + f" - {a}")
        # Llama al editor pasando la lista de archivos para que se abran en buffers (por ejemplo, en nvim).
        subprocess.run([editor_cmd] + archivos)
    else:
        print(Fore.YELLOW + "No se encontraron archivos modificados para editar.")

    # Extrae el mensaje original del commit y lo ubica en un fichero temporal.
    commit_message = run(f"git log -1 --pretty=format:%B {commit_ref}").strip()
    with tempfile.NamedTemporaryFile(mode="w+", delete=False) as msg_file:
        msg_file.write(commit_message)
        temp_msg_file = msg_file.name

    print(Fore.YELLOW + f"Abriendo el editor ({editor_cmd}) para editar el mensaje del commit...")
    subprocess.run([editor_cmd, temp_msg_file])

    # Agrega todos los cambios y crea el commit utilizando el mensaje editado.
    subprocess.run("git add .", shell=True)
    subprocess.run(f"git commit -F {temp_msg_file}", shell=True)
    os.unlink(temp_msg_file)

    applied_commits.add(commit)
    final_commits.append(commit)
    print(Fore.GREEN + f"Commit {commit} editado y aplicado correctamente.")

def ask_to_proceed():

    for commit in initial_commits:
        if commit not in final_commits:
            final_commits.append(commit)

    unique_commits = []
    for commit in final_commits:
        if commit not in unique_commits:
            unique_commits.append(commit)

    while True:
        print(Fore.CYAN + "\nCommits seleccionados para aplicar:")
        for i, c in enumerate(unique_commits, 1):
            context = get_commit_context(c)
            print(Fore.CYAN + f" {i}. {context}")

        if auto_mode:
            log_message("Modo automático: continuando con el cherry-pick", "INFO")
            apply_commits_in_order()
            break

        options = [
            f"Continuar con el cherry-pick ({len(unique_commits)})", 
            "Cancelar y revisar nuevamente",
            "Guardar lista y salir sin aplicar"
        ]
        choice = select_option(options, "Selecciona una opción: ")

        if choice.startswith("Continuar"):
            apply_commits_in_order()
            break
        elif choice.startswith("Guardar"):
            save_commits_list(unique_commits)
            print(Fore.GREEN + f"Se guardaron {len(unique_commits)} commits en '{COMMITS_LIST_FILE}'.")
            print(Fore.GREEN + "Puedes aplicarlos más tarde usando --apply-saved")
            break
        elif choice.startswith("Cancelar"):
            print(Fore.RED + "Revisión cancelada. Puedes volver a analizar los commits.")
            break

def apply_commits_in_order():
    # Asegurar que todos los commits iniciales estén incluidos
    for commit in initial_commits:
        if commit not in final_commits and commit not in skipped_commits:
            final_commits.append(commit)

    # Crear una lista ordenada de commits únicos
    unique_commits = OrderedDict()
    for commit in final_commits:
        if commit not in unique_commits and commit not in skipped_commits:
            unique_commits[commit] = None

    commit_list = list(unique_commits.keys())
    save_commits_list(commit_list)
    log_message(f"Se guardarán {len(commit_list)} commits en '{COMMITS_LIST_FILE}'", "INFO")

    if dry_run:
        print(Fore.YELLOW + f"[Modo simulación] Se aplicarían {len(commit_list)} commits:")
        for i, commit in enumerate(commit_list, 1):
            print(Fore.YELLOW + f"  {i}. {get_commit_context(commit)}")
        return

    total_commits = len(commit_list)
    batch_size = 10  # Procesar commits en lotes para mejorar rendimiento en proyectos grandes
    success_count = 0
    fail_count = 0
    
    # Estadísticas globales
    global_stats_key = start_operation_timer("batch", "apply_all_commits")
    if global_stats_key in stats_data:
        stats_data[global_stats_key]["file_count"] = total_commits
    
    for batch_start in range(0, total_commits, batch_size):
        batch_end = min(batch_start + batch_size, total_commits)
        batch = commit_list[batch_start:batch_end]
        
        print(Fore.CYAN + f"\nProcesando lote {batch_start//batch_size + 1}/{(total_commits+batch_size-1)//batch_size} ({batch_end-batch_start} commits)")
        
        for idx, commit in enumerate(batch):
            overall_idx = batch_start + idx
            show_progress(overall_idx + 1, total_commits, "Aplicando commits")

            if commit in applied_commits:
                log_message(f"Commit {commit} ya fue aplicado previamente. Saltando.", "INFO")
                success_count += 1
                continue

            if commit in skipped_commits:
                log_message(f"Commit {commit} está en la lista de commits a omitir. Saltando.", "INFO")
                continue

            log_message(f"Aplicando cherry-pick para {commit}", "INFO")
            op_key = start_operation_timer(commit, "cherry_pick")

            # Preparar la referencia del commit
            commit_ref = commit
            if remote_name:
                remote_ref = f"{remote_name}/{commit}"
                test_exists = run(f"git rev-parse --verify {remote_ref}^{{commit}} 2>/dev/null", allow_fail=True)
                if test_exists:
                    commit_ref = remote_ref

            # Intentar aplicar el commit
            print(Fore.GREEN + f"\n[{overall_idx+1}/{total_commits}] Aplicando cherry-pick --empty=drop {commit}...")
            
            # Verificar si es un proyecto grande y optimizar
            if total_commits > 50:  # Para proyectos grandes
                # Usamos --allow-empty para procesar rápidamente commits que podrían ser vacíos
                result = subprocess.run(f"git cherry-pick --allow-empty --empty=drop {commit_ref}", shell=True)
            else:
                result = subprocess.run(f"git cherry-pick --empty=drop {commit_ref}", shell=True)

            # Manejar el resultado
            if result.returncode != 0:
                fail_count += 1
                end_operation_timer(op_key, "failure")
                handle_cherry_pick_error(commit)
            else:
                applied_commits.add(commit)
                success_count += 1
                end_operation_timer(op_key, "success")
                log_message(f"Commit {commit} aplicado exitosamente.", "SUCCESS")
        
        # Si estamos en un proyecto grande, guardar el progreso después de cada lote
        if total_commits > 50:
            save_history(applied_commits)
            log_message(f"Progreso guardado: {success_count}/{total_commits} commits aplicados", "INFO")

    # Finalizar estadísticas globales
    if global_stats_key in stats_data:
        stats_data[global_stats_key]["success_count"] = success_count
        stats_data[global_stats_key]["fail_count"] = fail_count
    end_operation_timer(global_stats_key, "completed")
    
    # Mostrar resumen
    print("\n" + "="*50)
    print(Fore.GREEN + f"Resumen de Cherry-pick:")
    print(Fore.GREEN + f"  Total commits procesados: {total_commits}")
    print(Fore.GREEN + f"  Exitosos: {success_count}")
    if fail_count > 0:
        print(Fore.RED + f"  Fallidos: {fail_count}")
    print(Fore.GREEN + f"  Tiempo total: {int(time.time() - start_time)} segundos")
    print("="*50)

    save_history(applied_commits)

def parse_not_existing_files(error_output):
    not_exist_files = []
    pattern = r"error: ([^:]+): does not exist in index"
    matches = re.findall(pattern, error_output)

    for match in matches:
        not_exist_files.append(match.strip())

    return not_exist_files

def handle_cherry_pick_error(commit):
    op_key = start_operation_timer(commit, "error_resolution")
    resolution_method = "manual"
    
    # Intenta resolver el error automáticamente en modo auto
    if auto_mode:
        # Primero intentamos con --strategy-option=theirs
        log_message("Modo automático: intentando resolver conflictos con --strategy-option=theirs", "INFO")
        run("git cherry-pick --abort", allow_fail=True)
        result = subprocess.run(f"git cherry-pick --strategy-option=theirs {commit}", shell=True)
        if result.returncode == 0:
            applied_commits.add(commit)
            resolution_method = "auto_theirs"
            log_message(f"Commit {commit} aplicado automáticamente con estrategia 'theirs'", "SUCCESS")
            end_operation_timer(op_key, "success", resolution_method)
            return

        # Si falla, intentamos con --strategy-option=ours
        log_message("Falló resolución 'theirs', intentando con 'ours'", "INFO")
        run("git cherry-pick --abort", allow_fail=True)
        result = subprocess.run(f"git cherry-pick --strategy-option=ours {commit}", shell=True)
        if result.returncode == 0:
            applied_commits.add(commit)
            resolution_method = "auto_ours"
            log_message(f"Commit {commit} aplicado automáticamente con estrategia 'ours'", "SUCCESS")
            end_operation_timer(op_key, "success", resolution_method)
            return

    log_message(f"Error al aplicar el commit {commit}. Analizando conflictos...", "ERROR")

    error_output = run("git status 2>&1")
    not_exist_files = parse_not_existing_files(error_output)
    failed_files = run("git diff --name-only --diff-filter=U").splitlines()
    
    # Actualizar estadísticas con información de conflictos
    if op_key in stats_data:
        stats_data[op_key]["conflict_count"] = len(not_exist_files) + len(failed_files)

    if not_exist_files:
        print(Fore.YELLOW + f"Se encontraron {len(not_exist_files)} archivos que no existen en el índice:")
        for f in not_exist_files:
            print(Fore.YELLOW + f" - {f}")

        run("git cherry-pick --abort")
        resolution_method = "missing_files_handling"
        
        missing_handled_count = 0
        for missing_file in not_exist_files:
            if not ask_to_search_file(missing_file):
                log_message(f"Ignorando archivo faltante: {missing_file}", "WARNING")
                continue

            adding_commit = find_commit_adding_file(missing_file)
            if adding_commit:
                context = get_commit_context(adding_commit)
                print(Fore.CYAN + f"\nEl archivo '{missing_file}' fue agregado originalmente en:\n  {context}")

                if handle_missing_file(missing_file, adding_commit, commit):
                    missing_handled_count += 1
            else:
                log_message(f"No se pudo encontrar el commit que agregó '{missing_file}'", "WARNING")

        # Si no se pudo manejar ningún archivo faltante, terminar
        if missing_handled_count == 0 and len(not_exist_files) > 0:
            log_message("No se pudo manejar ningún archivo faltante", "ERROR")
            end_operation_timer(op_key, "failure", resolution_method)
            return

        print(Fore.GREEN + f"\nIntentando aplicar cherry-pick nuevamente para {commit}...")

        commit_ref = commit
        if remote_name:
            remote_ref = f"{remote_name}/{commit}"
            test_exists = run(f"git rev-parse --verify {remote_ref}^{{commit}} 2>/dev/null", allow_fail=True)
            if test_exists:
                commit_ref = remote_ref

        result = subprocess.run(f"git cherry-pick --empty=drop {commit_ref}", shell=True)
        if result.returncode == 0:
            applied_commits.add(commit)
            log_message(f"Commit {commit} aplicado exitosamente después de resolver archivos faltantes.", "SUCCESS")
            end_operation_timer(op_key, "success", resolution_method)
        else:
            log_message(f"Todavía hay problemas al aplicar el commit. Puedes intentar manualmente.", "ERROR")
            end_operation_timer(op_key, "failure", resolution_method)

    elif failed_files:
        print(Fore.YELLOW + f"Archivos con conflictos detectados ({len(failed_files)}):")
        for f in failed_files:
            print(Fore.YELLOW + f" - {f}")

        # En modo auto, intentar resolver usando estrategias automáticas primero
        if auto_mode:
            log_message(f"Modo automático: intentando resolver {len(failed_files)} conflictos", "INFO")
            resolution_method = "auto_conflict_resolution"
            
            # Primero intentamos usar git automáticamente
            result = run("git -c core.editor=true merge --continue 2>&1", allow_fail=True)
            if "use 'git add' to mark resolution" not in result:
                # Intentar añadir todos los archivos automáticamente
                for file in failed_files:
                    run(f"git add {file}", allow_fail=True)
                
                result = run("git cherry-pick --continue 2>&1", allow_fail=True)
                if "fixed-up" in result or "successfully" in result:
                    applied_commits.add(commit)
                    log_message(f"Conflictos resueltos automáticamente para {commit}", "SUCCESS")
                    end_operation_timer(op_key, "success", resolution_method)
                    return
            
            # Si eso falla, intentar manejar renombres
            run("git cherry-pick --abort", allow_fail=True)
            handled = True
            for file in failed_files:
                # Buscar archivo más similar
                similar_files = find_similar_files(file)
                if similar_files and similar_files[0][1] > 70:  # Si hay un archivo similar con score > 70
                    file_renames[file] = similar_files[0][0]
                    log_message(f"Auto-renombramiento: {file} -> {similar_files[0][0]}", "INFO")
                else:
                    handled = False
            
            if handled:
                if apply_patch_with_rename_handling(commit):
                    end_operation_timer(op_key, "success", "auto_rename_handling")
                    return
        
        # Si el modo auto falló o no estamos en modo auto
        options = [
            "Resolver conflictos manualmente", 
            "Intentar manejo automático de archivos renombrados", 
            "Abortar cherry-pick"
        ]
        choice = select_option(options)

        if choice.startswith("Resolver"):
            resolution_method = "manual_conflict_resolution"
            if resume_cherry_pick(failed_files):
                applied_commits.add(commit)
                end_operation_timer(op_key, "success", resolution_method)
            else:
                end_operation_timer(op_key, "failure", resolution_method)
        elif choice.startswith("Intentar"):
            resolution_method = "manual_rename_handling"
            run("git cherry-pick --abort")
            handled = ask_file_renames_from_errors(failed_files)
            if handled:
                if apply_patch_with_rename_handling(commit):
                    applied_commits.add(commit)
                    end_operation_timer(op_key, "success", resolution_method)
                else:
                    end_operation_timer(op_key, "failure", resolution_method)
            else:
                log_message("No se pudo manejar los renombres. Saltando commit.", "ERROR")
                end_operation_timer(op_key, "failure", resolution_method)
        else:
            resolution_method = "aborted"
            run("git cherry-pick --abort")
            log_message(f"Cherry-pick abortado para el commit {commit}.", "WARNING")
            end_operation_timer(op_key, "aborted", resolution_method)
    else:
        resolution_method = "unknown_error"
        run("git cherry-pick --abort")
        log_message(f"Cherry-pick fallido pero no se detectaron conflictos. Saltando commit.", "ERROR")
        end_operation_timer(op_key, "failure", resolution_method)

def get_current_branch():
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False
    ).stdout.strip()

def get_file_content_at_commit(file_path, commit, branch_name):

    branch_name = branch_name or get_current_branch()
    if verbose_mode:
        print(f"Rama actual: {branch_name}")
    
    # Intenta obtener el contenido directamente
    content = run(f"git show {commit}:{file_path}", allow_fail=True)
    if content:
       return content  
    
    # Si hay un remote, intenta obtenerlo desde allí
    if remote_name:
        if verbose_mode:
            log_message(f"Intentando obtener {file_path} de {commit} vía remote {remote_name}", "DEBUG")

        # Intenta hacer fetch del commit específico
        run(f"git fetch {remote_name} {commit}", allow_fail=True)
        
        # Intenta con la referencia remota completa
        remote_commit = f"{remote_name}/{commit}"
        content = run(f"git show {remote_commit}:{file_path}", allow_fail=True)
        if content:
           return content
           
        # Intenta buscar en otras ramas remotas
        remote_branches = run(f"git branch -r | grep {remote_name}/", allow_fail=True).splitlines()
        for remote_branch in remote_branches:
            content = run(f"git show {remote_branch}:{file_path}", allow_fail=True)
            if content:
                log_message(f"Archivo encontrado en rama remota: {remote_branch}", "INFO")
                return content

    # Busca en commits recientes
    recent_commits = run(f"git log -n 50 --pretty=format:'%H'", allow_fail=True).splitlines()
    for recent_commit in recent_commits:
        if recent_commit != commit:  # Evita intentar con el mismo commit
            content = run(f"git show {recent_commit}:{file_path}", allow_fail=True)
            if content:
                log_message(f"Se encontró el archivo en un commit reciente: {recent_commit[:8]}", "INFO")
                return content
    
    log_message(f"No se pudo obtener contenido de {file_path} en ninguna referencia", "WARNING")
    return None

def handle_missing_file(missing_file, adding_commit, current_commit):
    global created_files

    commit_chain = find_commit_history_chain(missing_file, current_commit)

    if not commit_chain:
        log_message(f"No se pudo encontrar la historia del archivo '{missing_file}'", "WARNING")
        return False

    print(Fore.CYAN + f"\nSe encontraron {len(commit_chain)} commits relacionados con '{missing_file}':")
    max_display = min(5, len(commit_chain))
    for i, commit in enumerate(commit_chain[:max_display], 1):
        print(Fore.CYAN + f" {i}. {get_commit_context(commit)}")

    if len(commit_chain) > max_display:
        print(Fore.CYAN + f"   ... y {len(commit_chain)-max_display} más.")

    options = [
        f"Agregar todos los {len(commit_chain)} commits que modificaron el archivo",
        f"Agregar solo el commit que creó el archivo ({commit_chain[0][:8]})",
        "Especificar un archivo local equivalente",
        "Crear el archivo desde cero",
        "Saltar este archivo"
    ]

    if auto_mode:
        log_message(f"Modo automático: agregando {len(commit_chain)} commits relacionados", "INFO")
        for commit in commit_chain:
            add_commit_once(commit)
        created_files.add(missing_file)
        return True

    choice = select_option(options)

    if choice.startswith("Agregar todos"):

        for commit in commit_chain:
            add_commit_once(commit)
            print(Fore.BLUE + f"Se agregó {commit} a la lista de commits a aplicar.")
        created_files.add(missing_file)
        return True

    elif choice.startswith("Agregar solo"):

        add_commit_once(adding_commit)
        print(Fore.BLUE + f"Se agregó {adding_commit} a la lista de commits a aplicar.")
        created_files.add(missing_file)
        return True

    elif choice.startswith("Especificar"):

        local_name = input(f"Archivo local para '{missing_file}': ").strip()
        file_renames[missing_file] = local_name
        save_file_renames()  
        return True

    elif choice.startswith("Crear"):

        commit_ref = current_commit
        if remote_name:
            remote_ref = f"{remote_name}/{current_commit}"
            test_exists = run(f"git rev-parse --verify {remote_ref}^{{commit}} 2>/dev/null", allow_fail=True)
            if test_exists:
                commit_ref = remote_ref

        try:

            file_content = get_file_content_at_commit(missing_file, current_commit, get_current_branch())
            if file_content:

                directory = os.path.dirname(missing_file)
                if directory and not os.path.exists(directory):
                    os.makedirs(directory, exist_ok=True)

                with open(missing_file, 'w') as f:
                    f.write(file_content)

                log_message(f"Archivo '{missing_file}' creado exitosamente.", "SUCCESS")

                run(f"git add {missing_file}")
                created_files.add(missing_file)
                return True
            else:
                log_message(f"No se pudo obtener el contenido del archivo '{missing_file}'.", "ERROR")
                return False
        except Exception as e:
            log_message(f"Error al crear el archivo: {str(e)}", "ERROR")
            return False

    else:

        log_message(f"Saltando archivo '{missing_file}'.", "WARNING")
        return False

def ask_file_renames_from_errors(failed_files):
    """
    Permite al usuario especificar rutas correctas para archivos que fallaron durante un cherry-pick.
    Maneja especialmente el caso de archivos eliminados en HEAD pero modificados en el commit.
    """
    if not failed_files:
        log_message("No hay archivos con problemas para renombrar.", "INFO")
        return False
    
    # Detectar archivos con conflictos modify/delete
    git_status = run("git status", allow_fail=True)
    modify_delete_files = []
    for archivo in failed_files:
        if f"{archivo} deleted in HEAD" in git_status or f"CONFLICT (modify/delete): {archivo}" in git_status:
            modify_delete_files.append(archivo)
    
    has_modify_delete = len(modify_delete_files) > 0
    if has_modify_delete:
        log_message(f"Detectados {len(modify_delete_files)} archivos con conflicto modify/delete", "INFO")
        
    # Para modo automático
    if auto_mode:
        log_message("Modo automático: intentando resolver conflictos de archivos", "INFO")
        rename_count = 0
        
        for archivo in failed_files:
            if archivo in modify_delete_files:
                # Si el archivo fue eliminado en HEAD pero modificado en el commit, recrearlo
                try:
                    # Intentar obtener contenido del archivo desde el commit a aplicar (MERGE_HEAD)
                    content = run(f"git show MERGE_HEAD:{archivo}", allow_fail=True)
                    if content:
                        # Crear directorios si es necesario
                        os.makedirs(os.path.dirname(archivo), exist_ok=True)
                        # Escribir el archivo
                        with open(archivo, "w") as f:
                            f.write(content)
                        run(f"git add {archivo}", allow_fail=True)
                        log_message(f"Archivo recreado automáticamente: {archivo}", "INFO")
                        rename_count += 1
                    else:
                        log_message(f"No se pudo obtener contenido para {archivo}", "WARNING")
                except Exception as e:
                    log_message(f"Error al recrear archivo {archivo}: {str(e)}", "ERROR")
            else:
                # Para el resto de archivos, intentar buscar similares
                similar_files = find_similar_files(archivo)
                if similar_files and similar_files[0][1] > 70:  # Si hay un archivo similar con score > 70%
                    file_renames[archivo] = similar_files[0][0]
                    log_message(f"Mapeo automático: {archivo} -> {similar_files[0][0]} (similitud: {similar_files[0][1]}%)", "INFO")
                    rename_count += 1
        
        save_file_renames()  # Guardar siempre los renombres, incluso parciales
        
        if rename_count == len(failed_files):
            log_message(f"Se resolvieron automáticamente todos los {len(failed_files)} archivos", "SUCCESS")
            return True
        elif rename_count > 0:
            log_message(f"Se resolvieron automáticamente {rename_count} de {len(failed_files)} archivos", "INFO")
            # Intentamos con los que se encontraron, tal vez es suficiente
            return True
        else:
            log_message("No se pudo resolver ningún archivo automáticamente", "WARNING")
            
    # Modo interactivo para usuario
    while True:
        print(Fore.RED + "\nCherry-pick fallido. Posibles errores por archivos con conflictos:")
        
        # Mostrar archivos con problemas
        for i, archivo in enumerate(failed_files, 1):
            ruta_actual = file_renames.get(archivo, archivo)
            tipo = "(modify/delete)" if archivo in modify_delete_files else ""
            print(Fore.YELLOW + f"{i}. {ruta_actual} {tipo}")
        
        # Opciones adicionales
        terminar_opcion = len(failed_files) + 1
        cancelar_opcion = len(failed_files) + 2
        print(Fore.GREEN + f"{terminar_opcion}. Continuar con las rutas modificadas")
        print(Fore.RED + f"{cancelar_opcion}. Cancelar y abortar el proceso")
        
        # Procesar la opción del usuario
        opcion = input(Fore.CYAN + "Selecciona una opción (número): ").strip()
        
        # Verificar si el usuario quiere terminar
        if opcion == str(terminar_opcion):
            # Verificar que al menos un archivo haya sido renombrado o manejado
            if any(archivo in file_renames for archivo in failed_files) or len(modify_delete_files) > 0:
                save_file_renames()  # Guardar los cambios
                return True
            else:
                continuar = input(Fore.YELLOW + "No se han especificado renombres. ¿Continuar de todos modos? (s/n): ").lower()
                if continuar.startswith('s'):
                    return True
                else:
                    continue  # Volver al bucle principal
        
        # Verificar si el usuario quiere cancelar
        if opcion == str(cancelar_opcion):
            return False
            
        # Procesar selección de archivo para renombrar
        try:
            idx = int(opcion)
            if 1 <= idx <= len(failed_files):
                archivo = failed_files[idx - 1]
                ruta_actual = file_renames.get(archivo, archivo)
                
                # Caso especial para archivos modify/delete
                if archivo in modify_delete_files:
                    print(Fore.CYAN + f"\nArchivo '{archivo}' fue eliminado en HEAD pero modificado en el commit.")
                    opciones = [
                        "Recrear el archivo con el contenido del commit",
                        "Ignorar los cambios del commit (mantener eliminado)",
                        "Especificar un archivo equivalente",
                        "Volver al menú principal"
                    ]
                    sel = select_option(opciones, "¿Qué deseas hacer? ")
                    
                    if sel.startswith("Recrear"):
                        try:
                            # Intentar obtener contenido del archivo desde el commit a aplicar
                            content = run(f"git show MERGE_HEAD:{archivo}", allow_fail=True)
                            if content:
                                # Crear directorios si es necesario
                                os.makedirs(os.path.dirname(archivo), exist_ok=True)
                                # Escribir el archivo
                                with open(archivo, "w") as f:
                                    f.write(content)
                                run(f"git add {archivo}", allow_fail=True)
                                log_message(f"Archivo recreado: {archivo}", "SUCCESS")
                            else:
                                log_message(f"No se pudo obtener contenido para {archivo}", "ERROR")
                        except Exception as e:
                            log_message(f"Error al recrear archivo {archivo}: {str(e)}", "ERROR")
                    elif sel.startswith("Ignorar"):
                        # No hacer nada, dejarlo eliminado
                        run(f"git rm {archivo}", allow_fail=True)
                        log_message(f"Ignorando cambios en archivo eliminado: {archivo}", "INFO")
                    elif sel.startswith("Especificar"):
                        nueva_ruta = input(Fore.CYAN + f"Ingrese la ruta de un archivo equivalente para '{archivo}': ").strip()
                        if nueva_ruta:
                            file_renames[archivo] = nueva_ruta
                            log_message(f"Renombramiento configurado: {archivo} -> {nueva_ruta}", "INFO")
                    continue
                
                # Mostrar archivos similares como sugerencia para archivos normales
                similar_files = find_similar_files(archivo)
                if similar_files:
                    print(Fore.CYAN + "\nArchivos similares encontrados:")
                    for i, (similar_path, score) in enumerate(similar_files[:5], 1):
                        print(Fore.CYAN + f"  {i}. {similar_path} (similitud: {score}%)")
                    
                    # Permitir selección directa de archivo similar
                    select_similar = input(Fore.CYAN + "¿Seleccionar un archivo similar por número o ingresar ruta manualmente? (número/m): ").strip()
                    try:
                        similar_idx = int(select_similar)
                        if 1 <= similar_idx <= len(similar_files[:5]):
                            file_renames[archivo] = similar_files[similar_idx-1][0]
                            log_message(f"Renombramiento configurado: {archivo} -> {similar_files[similar_idx-1][0]}", "INFO")
                            continue
                    except ValueError:
                        # No es un número, seguimos con entrada manual
                        pass
                
                # Solicitar nueva ruta
                nueva_ruta = input(Fore.CYAN + f"Ingrese la ruta correcta para '{ruta_actual}' (vacío para mantener): ").strip()
                
                if nueva_ruta:
                    file_renames[archivo] = nueva_ruta
                    log_message(f"Renombramiento configurado: {archivo} -> {nueva_ruta}", "INFO")
                else:
                    print(Fore.YELLOW + "No se ingresó ninguna ruta. Se mantiene la ruta actual.")
            else:
                print(Fore.RED + "Opción inválida. Intenta de nuevo.")
        except ValueError:
            print(Fore.RED + "Por favor, ingresa un número válido.")


def apply_patch_with_rename_handling(commit):
    """
    Aplica un cherry-pick manejando renombres de archivos especificados en file_renames.
    También maneja casos especiales como archivos eliminados en HEAD pero modificados en el commit.
    Genera un parche a partir del commit y lo modifica antes de aplicarlo.
    """
    op_key = start_operation_timer(commit, "patch_rename_handling")
    
    # Preparar referencia del commit
    commit_ref = commit
    if remote_name:
        remote_ref = f"{remote_name}/{commit}"
        test_exists = run(f"git rev-parse --verify {remote_ref}^{{commit}} 2>/dev/null", allow_fail=True)
        if test_exists:
            commit_ref = remote_ref

    print(Fore.GREEN + f"\nAplicando cherry-pick con manejo de renombres para {commit}...")
    
    # Recuperar el contexto original del cherry-pick para preservar los auto-merges
    original_status = run("git status -s", allow_fail=True)
    original_files = []
    for line in original_status.splitlines():
        if line.startswith("AU") or line.startswith("UU"):
            original_files.append(line.split()[-1])
    
    # Generar parche a partir del commit
    patch = run(f"git format-patch -1 {commit_ref} --stdout")
    if not patch.strip():
        log_message("El commit no tiene cambios. Marcando como aplicado.", "WARNING")
        return True  # No hay cambios, consideramos éxito
    
    # Verificar qué archivos se ven afectados en el parche
    affected_files = []
    deleted_files = []
    modified_files = []
    auto_merged_files = {} # Mapeo de archivos que fueron auto-mergeados
    
    # Extraer información de archivos auto-mergeados del mensaje git
    git_msg = run("git status", allow_fail=True)
    for line in git_msg.splitlines():
        if line.startswith("Auto-merging "):
            auto_file = line.replace("Auto-merging ", "").strip()
            # Buscar el archivo original en el parche
            for pline in patch.splitlines():
                if pline.startswith("diff --git") and auto_file in pline:
                    parts = pline.split()
                    orig_file = parts[2][2:] # Extraer ruta original
                    auto_merged_files[orig_file] = auto_file
                    log_message(f"Detectado auto-merge: {orig_file} -> {auto_file}", "INFO")
                    break
    
    # Identificar archivos afectados y sus estados
    for line in patch.splitlines():
        if line.startswith("--- a/"):
            path = line[6:].strip()
            if path != "/dev/null" and path not in affected_files:
                affected_files.append(path)
                
                # Verificar si el archivo existe en el índice actual
                exists = run(f"git ls-files --error-unmatch {path} 2>/dev/null || echo 'NOT_EXISTS'", allow_fail=True)
                if exists.strip() == "NOT_EXISTS":
                    deleted_files.append(path)
                else:
                    modified_files.append(path)
                    
        elif line.startswith("+++ b/"):
            path = line[6:].strip()
            if path != "/dev/null" and path not in affected_files:
                affected_files.append(path)
    
    log_message(f"Commit afecta a {len(affected_files)} archivos", "INFO")
    if deleted_files:
        log_message(f"Archivos eliminados en HEAD pero modificados en commit: {len(deleted_files)}", "INFO")
    
    # Abortar cualquier cherry-pick en progreso para reiniciar limpio
    run("git cherry-pick --abort", allow_fail=True)
    
    # Intentar recrear archivos eliminados automáticamente si es necesario
    for file_path in deleted_files:
        try:
            # Intentar obtener contenido del archivo desde el commit
            content = run(f"git show {commit_ref}:{file_path}", allow_fail=True)
            if content:
                # Crear directorios si es necesario
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
                # Escribir el archivo
                with open(file_path, 'w') as f:
                    f.write(content)
                # Añadir al índice
                run(f"git add {file_path}", allow_fail=True)
                log_message(f"Archivo recreado desde commit: {file_path}", "INFO")
        except Exception as e:
            log_message(f"Error al recrear archivo eliminado {file_path}: {str(e)}", "ERROR")
    
    # Buscar y aplicar renombres automáticamente si es necesario
    for file_path in affected_files:
        # Primero verificar si este archivo tenía un auto-merge
        if file_path in auto_merged_files:
            target_file = auto_merged_files[file_path]
            log_message(f"Preservando auto-merge: {file_path} -> {target_file}", "INFO")
            file_renames[file_path] = target_file
        # Si no, aplicar renombre automático si es necesario
        elif file_path not in file_renames and file_path not in deleted_files and not os.path.exists(file_path):
            similar_files = find_similar_files(file_path)
            if similar_files and similar_files[0][1] > 80:  # Alta similitud (80%)
                file_renames[file_path] = similar_files[0][0]
                log_message(f"Renombre automático: {file_path} -> {similar_files[0][0]}", "INFO")
    
    # Reemplazar rutas en el parche con los renombres definidos
    modified_patch = patch
    for origen, destino in file_renames.items():
        # Reemplazar en líneas de diff (--- a/ruta, +++ b/ruta)
        modified_patch = modified_patch.replace(f"--- a/{origen}", f"--- a/{destino}")
        modified_patch = modified_patch.replace(f"+++ b/{origen}", f"+++ b/{destino}")
        # Reemplazar en headers de diff (diff --git a/ruta b/ruta)
        modified_patch = modified_patch.replace(f"diff --git a/{origen} b/{origen}", 
                                             f"diff --git a/{destino} b/{destino}")
        # Reemplazar en otras líneas (como mensajes)
        modified_patch = modified_patch.replace(f" {origen}", f" {destino}")
    
    # Escribir parche modificado a archivo temporal
    with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmpfile:
        tmpfile.write(modified_patch)
        tmpfile_path = tmpfile.name
    
    # Intentar aplicar el parche modificado con opciones más tolerantes
    apply_options = "--3way --index"  # Use 3-way merge para mejor manejo de conflictos
    if deleted_files:
        apply_options += " --allow-empty"  # Permitir parches vacíos
    
    log_message(f"Aplicando parche con opciones: {apply_options}", "INFO")
    result = subprocess.run(f"git apply {apply_options} {tmpfile_path}", shell=True)
    
    # Limpiar archivo temporal
    os.unlink(tmpfile_path)
    
    # Verificar si hay conflictos
    conflict_files = run("git diff --name-only --diff-filter=U", allow_fail=True).splitlines()
    if conflict_files:
        # Hay conflictos que requieren edición manual
        log_message(f"Encontrados {len(conflict_files)} archivos con conflictos para resolver manualmente", "WARNING")
        print(Fore.YELLOW + "Se encontraron conflictos que requieren resolución manual:")
        for f in conflict_files:
            print(Fore.CYAN + f" - {f}")
        
        # Editar archivos con nvim para resolver conflictos manualmente
        editor_cmd = get_preferred_editor()
        print(Fore.YELLOW + f"\nSe abrirán {len(conflict_files)} archivos con conflictos para edición manual")
        print(Fore.YELLOW + "Por favor, resuelve los conflictos en cada archivo y guarda los cambios")
        
        # Abrir cada archivo conflictivo uno por uno
        for i, file_path in enumerate(conflict_files, 1):
            print(Fore.CYAN + f"\nAbriendo archivo {i}/{len(conflict_files)}: {file_path}")
            print(Fore.CYAN + "Edita el archivo para resolver los conflictos y guárdalo")
            
            subprocess.run([editor_cmd, file_path], check=False)
        
        # Preguntar si desea continuar después de resolver los conflictos
        if not auto_mode:
            confirm = input(Fore.CYAN + "\n¿Has resuelto todos los conflictos y deseas continuar? (s/n): ").lower()
            if not confirm.startswith('s'):
                log_message("Resolución manual cancelada por el usuario", "WARNING")
                return False
        
        # Agregar todos los archivos modificados
        print(Fore.GREEN + "Agregando todos los archivos modificados...")
        run("git add -A", allow_fail=True)
        
        # Verificar si quedan conflictos sin resolver
        remaining_conflicts = run("git diff --name-only --diff-filter=U", allow_fail=True).splitlines()
        if remaining_conflicts:
            log_message(f"Aún quedan {len(remaining_conflicts)} archivos con conflictos sin resolver", "ERROR")
            print(Fore.RED + "Aún hay conflictos sin resolver en los siguientes archivos:")
            for f in remaining_conflicts:
                print(Fore.RED + f" - {f}")
            
            # Dar opción de volver a editar o cancelar
            if not auto_mode:
                retry = input(Fore.CYAN + "¿Deseas volver a editar estos archivos? (s/n): ").lower()
                if retry.startswith('s'):
                    # Volver a abrir los archivos con conflictos
                    for i, file_path in enumerate(remaining_conflicts, 1):
                        print(Fore.CYAN + f"\nAbriendo archivo {i}/{len(remaining_conflicts)}: {file_path}")
                        subprocess.run([editor_cmd, file_path], check=False)
                    
                    # Agregar todos los archivos modificados nuevamente
                    run("git add -A", allow_fail=True)
                    
                    # Verificar nuevamente si quedan conflictos
                    if run("git diff --name-only --diff-filter=U", allow_fail=True).strip():
                        log_message("Aún hay conflictos sin resolver después del segundo intento", "ERROR")
                        return False
                else:
                    log_message("Usuario canceló la resolución de conflictos restantes", "WARNING")
                    return False
            else:
                log_message("Modo automático: no se pudieron resolver todos los conflictos", "ERROR")
                return False
        
        # Crear commit con mensaje original
        message = run(f"git log -1 --pretty=format:%B {commit_ref}").strip()
        with tempfile.NamedTemporaryFile(mode="w+", delete=False) as msg_file:
            msg_file.write(message)
            msg_file_path = msg_file.name
        
        print(Fore.GREEN + "Creando commit con los cambios resueltos...")
        commit_result = subprocess.run(f"git commit -F {msg_file_path}", shell=True)
        os.unlink(msg_file_path)
        
        if commit_result.returncode == 0:
            log_message(f"Commit {commit} aplicado exitosamente después de resolución manual", "SUCCESS")
            end_operation_timer(op_key, "success", "manual_resolution")
            return True
        else:
            log_message("Error al crear commit después de resolver conflictos manualmente", "ERROR")
            end_operation_timer(op_key, "failure", "commit_error")
            return False
    elif result.returncode != 0:
        # Error al aplicar parche sin conflictos detectados
        log_message("Error al aplicar parche sin conflictos detectados", "ERROR")
        
        # Intento alternativo para archivos eliminados en HEAD
        log_message("Intentando método alternativo para archivos eliminados en HEAD", "INFO")
        # Limpiar cualquier cambio parcial
        run("git reset --hard", allow_fail=True)
        
        # Recrear archivos eliminados del commit
        success = True
        for file_path in deleted_files:
            try:
                # Extraer el contenido completo del archivo en el commit
                content = run(f"git show {commit_ref}:{file_path}", allow_fail=True)
                if not content:
                    log_message(f"No se pudo obtener contenido para {file_path}", "ERROR")
                    success = False
                    continue
                    
                # Crear directorios si no existen
                directory = os.path.dirname(file_path)
                if directory and not os.path.exists(directory):
                    os.makedirs(directory, exist_ok=True)
                
                # Escribir el archivo completo
                with open(file_path, 'w') as f:
                    f.write(content)
                
                # Añadir al índice
                run(f"git add {file_path}", allow_fail=True)
                log_message(f"Archivo recreado exitosamente: {file_path}", "SUCCESS")
            except Exception as e:
                log_message(f"Error al recrear archivo {file_path}: {str(e)}", "ERROR")
                success = False
        
        if success:
            # Crear commit con mensaje original
            message = run(f"git log -1 --pretty=format:%B {commit_ref}").strip()
            with tempfile.NamedTemporaryFile(mode="w+", delete=False) as msg_file:
                msg_file.write(message)
                msg_file_path = msg_file.name
            
            commit_result = subprocess.run(f"git commit -F {msg_file_path}", shell=True)
            os.unlink(msg_file_path)
            
            if commit_result.returncode == 0:
                log_message("Commit aplicado mediante recreación de archivos eliminados", "SUCCESS")
                end_operation_timer(op_key, "success", "recreate_files")
                return True
            else:
                log_message("Error al crear commit después de recrear archivos", "ERROR")
                end_operation_timer(op_key, "failure", "commit_error")
                return False
        else:
            log_message("No se pudieron recrear todos los archivos necesarios", "ERROR")
            end_operation_timer(op_key, "failure", "recreation_failed")
            return False
    else:
        # Parche aplicado correctamente sin conflictos
        log_message("Parche aplicado correctamente sin conflictos", "SUCCESS")
        
        # Verificar si hay cambios que requieran commit
        result = subprocess.run("git diff --cached --quiet", shell=True)
        if result.returncode != 0:  # Hay cambios
            # Obtener mensaje original
            message = run(f"git log -1 --pretty=format:%B {commit_ref}").strip()
            try:
                # Crear commit usando el mensaje original
                with tempfile.NamedTemporaryFile(mode="w+", delete=False) as msg_file:
                    msg_file.write(message)
                    msg_file_path = msg_file.name
                
                print(Fore.GREEN + "Creando commit con los cambios aplicados...")
                commit_result = subprocess.run(f"git commit -F {msg_file_path}", shell=True)
                os.unlink(msg_file_path)
                
                if commit_result.returncode != 0:
                    # Fallback con mensaje simple
                    log_message("Error al crear commit con mensaje original. Usando mensaje simple.", "WARNING")
                    subprocess.run(f"git commit -m \"Cherry-pick {commit} (con manejo de renombres)\"", shell=True)
            except Exception as e:
                log_message(f"Error al crear commit: {str(e)}", "ERROR")
                subprocess.run(f"git commit -m \"Cherry-pick {commit}\"", shell=True)
        else:
            log_message("No hay cambios para hacer commit después de aplicar el parche.", "WARNING")
    
    # Marcar como exitoso y guardar estadísticas
    end_operation_timer(op_key, "success", "rename_handling")
    log_message(f"Commit {commit} aplicado exitosamente con manejo de renombres.", "SUCCESS")
    save_file_renames()  # Guardar renombres para futuros commits
    return True



def get_preferred_editor():

    if config["default_editor"]:
        return config["default_editor"]

    if os.path.exists("/data/data/com.termux/files/usr/bin/nvim"):
        return "nvim"
    elif os.path.exists("/data/data/com.termux/files/usr/bin/vim"):
        return "vim"

    return os.environ.get("EDITOR", "vi")

def resume_cherry_pick(conflicted_files):

    if not conflicted_files:
        conflicted_files = run("git diff --name-only --diff-filter=U").splitlines()

    if not conflicted_files:
        log_message("No se encontraron archivos con conflictos.", "WARNING")
        return False

    editor_cmd = get_preferred_editor()

    print(Fore.YELLOW + f"Se abrirán {len(conflicted_files)} archivos CON CONFLICTOS uno a la vez.")

    for i, file in enumerate(conflicted_files, 1):
        print(Fore.CYAN + f"\nAbriendo archivo {i}/{len(conflicted_files)}: {file}")
        print(Fore.CYAN + "Edita el archivo para resolver los conflictos y guárdalo con :wq")

        subprocess.run(f"{editor_cmd} {file}", shell=True)

        if auto_mode:
            subprocess.run(f"git add {file}", shell=True)
            log_message(f"Archivo '{file}' añadido automáticamente.", "INFO")
        else:
            add_now = input(Fore.CYAN + f"¿Deseas añadir '{file}' ahora con 'git add'? (s/n): ").lower()
            if add_now.startswith('s'):
                subprocess.run(f"git add {file}", shell=True)
                log_message(f"Archivo '{file}' añadido correctamente.", "SUCCESS")

    remaining = run("git diff --name-only --diff-filter=U").splitlines()
    if remaining:
        print(Fore.YELLOW + f"Todavía quedan {len(remaining)} archivos con conflictos sin resolver:")
        for file in remaining:
            print(f" - {file}")

        if auto_mode:
            log_message("Modo automático: resolviendo archivos restantes", "INFO")
            return resume_cherry_pick(remaining)
        else:
            resolve_rest = input(Fore.CYAN + "¿Quieres resolver estos archivos también? (s/n): ").lower()
            if resolve_rest.startswith('s'):
                return resume_cherry_pick(remaining)

    print(Fore.GREEN + "\nTodos los conflictos parecen resueltos.")

    if auto_mode:
        subprocess.run("git add .", shell=True)
        log_message("Se añadieron todos los archivos automáticamente.", "INFO")
    else:
        add_all = input(Fore.CYAN + "¿Deseas hacer 'git add .' para añadir todos los cambios restantes? (s/n): ").lower()
        if add_all.startswith('s'):
            subprocess.run("git add .", shell=True)

    print(Fore.GREEN + "Continuando cherry-pick...")
    result = subprocess.run("git cherry-pick --continue", shell=True)
                content = run(f"git show {commit_ref}:{file_path}", allow_fail=True)
                if not content:
                    log_message(f"No se pudo obtener contenido para {file_path}", "ERROR")samente.", "SUCCESS")
                    success = False
                    continue
                    inuar cherry-pick.", "ERROR")
                # Crear directorios si no existen
                directory = os.path.dirname(file_path)
                if directory and not os.path.exists(directory):essage("2. git cherry-pick --continue", "INFO")
                    os.makedirs(directory, exist_ok=True)
                
                # Escribir el archivo completo
                with open(file_path, 'w') as f:
                    f.write(content)
                
                # Añadir al índiceore.CYAN + f" - {get_commit_context(c)}")
                run(f"git add {file_path}", allow_fail=True)
                log_message(f"Archivo recreado exitosamente: {file_path}", "SUCCESS")def get_commit_range(start_commit, end_commit):
            except Exception as e:    if remote_name:
                log_message(f"Error al recrear archivo {file_path}: {str(e)}", "ERROR")mote_name}")
                success = False
        
        if success and modify_delete_files:
            # Crear commit con mensaje original
            message = run(f"git log -1 --pretty=format:%B {commit_ref}").strip()
            with tempfile.NamedTemporaryFile(mode="w+", delete=False) as msg_file:= f"{remote_name}/{start_commit}"
                msg_file.write(message)start}^{{commit}} 2>/dev/null", allow_fail=True)
                msg_file_path = msg_file.nameart_exists:
                        start_ref = remote_start
            commit_result = subprocess.run(f"git commit -F {msg_file_path}", shell=True)
            os.unlink(msg_file_path)        remote_end = f"{remote_name}/{end_commit}"
            -parse --verify {remote_end}^{{commit}} 2>/dev/null", allow_fail=True)
            if commit_result.returncode == 0:        if remote_end_exists:
                log_message("Commit aplicado mediante recreación de archivos eliminados", "SUCCESS")_end
                end_operation_timer(op_key, "success", "recreate_files")
                return True    start_exists = run(f"git rev-parse --verify {start_ref}^{{commit}} 2>/dev/null")
            else:
                log_message("Error al crear commit después de recrear archivos", "ERROR")ERROR")
                end_operation_timer(op_key, "failure", "commit_error")
                return False
        else:verify {end_ref}^{{commit}} 2>/dev/null")
            log_message("No se pudieron manejar todos los casos de modify/delete", "ERROR")    if not end_exists:
            end_operation_timer(op_key, "failure", "cannot_handle")
            return False        sys.exit(1)
    else:
        # Parche aplicado correctamente sin conflictos
        log_message("Parche aplicado correctamente sin conflictos", "SUCCESS")
            if not commits:
        # Verificar si hay cambios que requieran commit especificado.", "ERROR")
        result = subprocess.run("git diff --cached --quiet", shell=True)        sys.exit(1)
        if result.returncode != 0:  # Hay cambios
            # Obtener mensaje original
            message = run(f"git log -1 --pretty=format:%B {commit_ref}").strip()
            try:():
                # Crear commit usando el mensaje original
                with tempfile.NamedTemporaryFile(mode="w+", delete=False) as msg_file:
                    msg_file.write(message)t_hash2 ... commit_hashN] [opciones]")
                    msg_file_path = msg_file.namet> [opciones]")
                    print("  python3 smart-chery-pick.py --apply-saved [opciones]")
                print(Fore.GREEN + "Creando commit con los cambios aplicados...")
                commit_result = subprocess.run(f"git commit -F {msg_file_path}", shell=True)mentos:")
                os.unlink(msg_file_path)
                      Especifica un rango de commits para aplicar")
                if commit_result.returncode != 0:     Commit inicial (más antiguo) del rango")
                    # Fallback con mensaje simple    print("    end_commit           Commit final (más reciente) del rango")
                    log_message("Error al crear commit con mensaje original. Usando mensaje simple.", "WARNING"):")
    print("  --remote REMOTE_NAME   Nombre del remote de Git (ej: origin, upstream, rem2)")
    print("  --skip-commit COMMITS  Lista de commits a omitir cuando se usa --range-commits")
    print("  --auto                 Modo automático (usa opciones por defecto sin preguntar)")
    print("  --verbose              Modo verboso (muestra más información)")
    print("  --dry-run              Simula el proceso sin aplicar cambios")
    print("  --apply-saved          Aplica los commits guardados previamente")
    print("  --config KEY=VALUE     Establece opciones de configuración")
    print("  --no-stats             Desactiva el registro de estadísticas")
    print("  --help                 Muestra este mensaje de ayuda")
    print("\nConfiguración:")
    print("  max_commits_display    Número máximo de commits a mostrar en listas")
    print("  max_search_depth       Profundidad máxima para buscar dependencias")
    print("  rename_detection_threshold Umbral para detección de archivos renombrados (0-100)")
    print("  auto_add_dependencies  Agregar automáticamente dependencias encontradas (true/false)")
    print("  show_progress_bar      Mostrar barra de progreso (true/false)")
    print("  max_retries            Número máximo de reintentos para comandos fallidos")
    print("  retry_delay            Retraso en segundos entre reintentos")
    print("  record_stats           Registrar estadísticas en CSV (true/false)")
    print("\nEjemplos:")
    print("  python3 smart-chery-pick.py abc1234")
    print("  python3 smart-chery-pick.py abc1234 def5678 --remote origin")
    print("  python3 smart-chery-pick.py --range-commits abc1234 def5678 --remote rem2")
    print("  python3 smart-chery-pick.py --range-commits abc1234 def5678 --skip-commit ghi9012 jkl3456")
    print("  python3 smart-chery-pick.py --auto --range-commits abc1234 def5678")
    print("  python3 smart-chery-pick.py --config max_search_depth=50 auto_add_dependencies=true")

def validate_remote(remote):
    """Valida y actualiza la información de un remote Git."""
    remotes = run("git remote").splitlines()
    if remote not in remotes:
        print(Fore.RED + f"Error: El remote '{remote}' no existe.")
        print(Fore.YELLOW + "Remotes disponibles:")
        for r in remotes:
            print(f" - {r}")

        log_message(f"El remote '{remote}' no existe. Usa uno de los remotos existentes.", "ERROR")
        sys.exit(1)

    remote_url = run(f"git remote get-url {remote}")
    log_message(f"Usando remote '{remote}' ({remote_url})", "INFO")

    print(Fore.CYAN + f"Actualizando información del remote '{remote}'...")
    run(f"git fetch {remote}", capture_output=False)
    return True

def update_config_from_args(config_args):
    global config
    for arg in config_args:
        if "=" in arg:
            key, value = arg.split("=", 1)
            if key in config:

                if isinstance(config[key], bool):
                    config[key] = value.lower() in ["true", "yes", "1", "t", "y"]
                elif isinstance(config[key], int):
                    config[key] = int(value)
                elif isinstance(config[key], float):
                    config[key] = float(value)
                else:
                    config[key] = value
                log_message(f"Configuración actualizada: {key}={value}", "INFO")
            else:
                log_message(f"Configuración desconocida: {key}", "WARNING")
    save_config()

def main():
    global applied_commits, stop_analysis, cherry_pick_queue, final_commits, analyzed_commits
    global initial_commit, initial_commits, author_map, skipped_commits, remote_name, auto_mode
    global verbose_mode, dry_run, file_renames

    parser = argparse.ArgumentParser(description='Smart Cherry Pick - Herramienta para aplicar commits de manera inteligente', add_help=False)
    parser.add_argument('commits', nargs='*', help='Commits a aplicar')
    parser.add_argument('--range-commits', nargs=2, metavar=('START_COMMIT', 'END_COMMIT'), help='Especifica un rango de commits para aplicar')
    parser.add_argument('--skip-commit', nargs='+', metavar='COMMIT', help='Commits a omitir cuando se usa --range-commits')
    parser.add_argument('--remote', metavar='REMOTE_NAME', help='Nombre del remote donde buscar commits')
    parser.add_argument('--auto', action='store_true', help='Modo automático (usa opciones por defecto sin preguntar)')
    parser.add_argument('--verbose', action='store_true', help='Modo verboso (muestra más información)')
    parser.add_argument('--dry-run', action='store_true', help='Simula el proceso sin aplicar cambios')
    parser.add_argument('--apply-saved', action='store_true', help='Aplica los commits guardados previamente')
    parser.add_argument('--config', nargs='+', metavar='KEY=VALUE', help='Establece opciones de configuración')
    parser.add_argument('--help', action='store_true', help='Muestra este mensaje de ayuda')
    parser.add_argument('--no-stats', action='store_true', help='No registrar estadísticas')

    try:
        args = parser.parse_args()
    except SystemExit:
        show_help()
        return

    if args.help or (not args.commits and not args.range_commits and not args.apply_saved):
        show_help()
        return

    # Configuración de ejecución
    if args.auto:
        auto_mode = True
    if args.verbose:
        verbose_mode = True
    if args.dry_run:
        dry_run = True
        print(Fore.YELLOW + "Modo simulación activado. No se aplicarán cambios reales.")
    
    # Cargar y actualizar configuración
    load_config()
    if args.config:
        update_config_from_args(args.config)
    
    # Configuración de estadísticas
    if args.no_stats:
        config["record_stats"] = False
    
    # Inicialización de archivos
    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "w").close()
    
    # Inicializar estadísticas si están habilitadas
    if config["record_stats"]:
        init_stats_file()

    log_message("Iniciando Smart Cherry Pick", "INFO")

    if args.remote:
        remote_name = args.remote
        log_message(f"Usando remote '{remote_name}' para buscar commits y archivos.", "INFO")
        # Validar si el remote existe, y si no, preguntar si desea crearlo
        remotes = run("git remote").splitlines()
        if remote_name not in remotes:
            print(Fore.YELLOW + f"El remote '{remote_name}' no existe.")
            if not auto_mode:
                create_remote = input(Fore.CYAN + f"¿Deseas agregar el remote '{remote_name}'? (URL del repositorio o 'n' para cancelar): ").strip()
                if create_remote.lower() != 'n' and create_remote:
                    try:
                        run(f"git remote add {remote_name} {create_remote}", capture_output=False)
                        log_message(f"Remote '{remote_name}' agregado con URL {create_remote}", "SUCCESS")
                        validate_remote(remote_name)
                    except Exception as e:
                        log_message(f"Error al agregar el remote: {str(e)}", "ERROR")
                        sys.exit(1)
                else:
                    log_message(f"Operación cancelada. No se agregó el remote '{remote_name}'", "WARNING")
                    sys.exit(1)
            else:
                log_message(f"Remote '{remote_name}' no existe y modo automático activado. Abortando.", "ERROR")
                sys.exit(1)
        else:
            validate_remote(remote_name)

    author_map = load_author_map()
    applied_commits = load_history()
    dep_cache = load_dep_cache()
    file_renames = load_file_renames()

    if args.apply_saved:
        saved_commits = load_commits_list()
        if not saved_commits:
            log_message("No hay commits guardados para aplicar.", "ERROR")
            return

        log_message(f"Aplicando {len(saved_commits)} commits guardados.", "INFO")
        initial_commits = saved_commits

    elif args.range_commits:
        start_commit, end_commit = args.range_commits

        if args.skip_commit:
            skipped_commits = set(args.skip_commit)
            log_message(f"Se omitirán {len(skipped_commits)} commits: {', '.join(list(skipped_commits)[:3])}...", "INFO")

        log_message(f"Obteniendo commits desde {start_commit} hasta {end_commit}...", "INFO")
        commit_range = get_commit_range(start_commit, end_commit)

        initial_commits = [c for c in commit_range if c not in skipped_commits]

        log_message(f"Se encontraron {len(commit_range)} commits en el rango, {len(initial_commits)} después de filtrar.", "INFO")
        print(Fore.CYAN + "Commits a aplicar (primeros 5):")
        for i, c in enumerate(initial_commits[:5], 1):
            print(Fore.CYAN + f"  {i}. {get_commit_context(c)}")

        if len(initial_commits) > 5:
            print(Fore.CYAN + f"     ... y {len(initial_commits)-5} más.")

    else:
        if not args.commits:
            log_message("Error: Debe especificar al menos un commit.", "ERROR")
            show_help()
            return

        initial_commits = args.commits
        log_message(f"Se aplicarán {len(initial_commits)} commits especificados.", "INFO")

    save_commits_list(initial_commits)

    stop_analysis = False
    cherry_pick_queue = []
    final_commits = []
    analyzed_commits = set()

    # Procesar cada commit
    total_start_time = time.time()
    try:
        for commit in initial_commits:
            if commit in applied_commits:
                log_message(f"El commit {commit} ya fue aplicado anteriormente. Saltando.", "INFO")
                continue

            if commit in skipped_commits:
                log_message(f"El commit {commit} está en la lista de commits a omitir. Saltando.", "INFO")
                continue

            initial_commit = commit
            stop_analysis = False
            cherry_pick_queue = [commit]

            process_commit(commit, dep_cache)

        if final_commits and not all(c in applied_commits for c in final_commits):
            ask_to_proceed()

    except KeyboardInterrupt:
        print(Fore.RED + "\nOperación interrumpida por el usuario.")
        # Guardar el progreso actual
        save_history(applied_commits)
        save_dep_cache(dep_cache)
        save_author_map(author_map)
        print(Fore.YELLOW + "Se ha guardado el progreso. Puedes retomar más tarde con --apply-saved.")
        sys.exit(1)
    finally:
        # Guardar estado final
        save_history(applied_commits)
        save_dep_cache(dep_cache)
        save_author_map(author_map)

        elapsed_time = int(time.time() - start_time)
        log_message(f"Proceso completado en {elapsed_time} segundos.", "SUCCESS")
        print(Fore.GREEN + f"\nProceso completado en {elapsed_time} segundos.")

if __name__ == "__main__":
    main()