import subprocess
import tempfile
import os
import sys
import json
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

config = {
    "max_commits_display": 5,
    "max_search_depth": 100,
    "rename_detection_threshold": 50,  
    "default_editor": None,  
    "auto_add_dependencies": False,
    "show_progress_bar": True,
    "max_retries": 3,
    "retry_delay": 2,  
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
    """Registra un mensaje en el archivo de log con timestamp."""
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

def load_config():
    """Carga la configuración desde el archivo de configuración."""
    global config
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                loaded_config = json.load(f)
                config.update(loaded_config)
        except Exception as e:
            log_message(f"Error al cargar configuración: {str(e)}", "ERROR")

def save_config():
    """Guarda la configuración actual en el archivo de configuración."""
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

def run(cmd, capture_output=True, input_text=None, retry=0, allow_fail=False):
    """Ejecuta un comando de shell con reintento automático en caso de fallo."""

    if remote_name and f"{remote_name}/" in cmd:

        if ("git log" in cmd and ".." in cmd):
            if verbose_mode:
                log_message(f"Detectado comando de rango git log con remote: {cmd}", "DEBUG")

            try:
                if verbose_mode:
                    log_message(f"Ejecutando: {cmd}", "DEBUG")
                result = subprocess.run(cmd, shell=True, capture_output=capture_output, text=True, input=input_text)

                if result.returncode != 0:

                    local_cmd = cmd.replace(f"{remote_name}/", "")
                    if verbose_mode:
                        log_message(f"Comando remoto falló, intentando con referencias locales: {local_cmd}", "DEBUG")
                    return run(local_cmd, capture_output, input_text, retry, allow_fail)

                return result.stdout.strip() if capture_output else None
            except Exception as e:

                local_cmd = cmd.replace(f"{remote_name}/", "")
                if verbose_mode:
                    log_message(f"Error con comando remoto, intentando con referencias locales: {local_cmd}", "DEBUG")
                return run(local_cmd, capture_output, input_text, retry, allow_fail)

        elif "git show" in cmd and ":" in cmd:
            if verbose_mode:
                log_message(f"Detectado comando git show con remote: {cmd}", "DEBUG")

            try:
                if verbose_mode:
                    log_message(f"Ejecutando: {cmd}", "DEBUG")

                result = subprocess.run(cmd, shell=True, capture_output=capture_output, text=True, input=input_text)

                if result.returncode != 0:

                    parts = cmd.split(":")
                    if len(parts) >= 2:
                        commit_ref = parts[0].split(" ")[-1]
                        file_path = ":".join(parts[1:])

                        if remote_name:
                            fetch_cmd = f"git fetch {remote_name} {commit_ref}"
                            if verbose_mode:
                                log_message(f"Intentando hacer fetch del commit {commit_ref} desde {remote_name}", "DEBUG")
                            subprocess.run(fetch_cmd, shell=True, capture_output=True, text=True)

                        local_commit = commit_ref.replace(f"{remote_name}/", "")
                        local_cmd = f"git show {local_commit}:{file_path}"
                        if verbose_mode:
                            log_message(f"Comando show remoto falló, intentando con referencia local: {local_cmd}", "DEBUG")

                        return run(local_cmd, capture_output, input_text, 0, allow_fail)

                return result.stdout.strip() if capture_output else None

            except Exception as e:

                if allow_fail:
                    return ""
                if verbose_mode:
                    log_message(f"Error con comando git show: {str(e)}", "ERROR")
                return ""

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
    """Presenta opciones al usuario y devuelve la selección."""
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
    """Carga el historial de commits ya aplicados."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_history(commits):
    """Guarda el historial de commits aplicados."""
    with open(HISTORY_FILE, "w") as f:
        json.dump(sorted(list(commits)), f, indent=2)

def load_dep_cache():
    """Carga la caché de dependencias de commits."""
    if os.path.exists(COMMIT_DEP_CACHE):
        with open(COMMIT_DEP_CACHE, "r") as f:
            return json.load(f)
    return {}

def save_dep_cache(cache):
    """Guarda la caché de dependencias de commits."""
    with open(COMMIT_DEP_CACHE, "w") as f:
        json.dump(cache, f, indent=2)

def load_author_map():
    """Carga el mapeo de autores a nombres de usuario."""
    if os.path.exists(AUTHOR_MAP_CACHE):
        try:
            with open(AUTHOR_MAP_CACHE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}

def save_author_map(map_data):
    """Guarda el mapeo de autores a nombres de usuario."""
    with open(AUTHOR_MAP_CACHE, "w") as f:
        json.dump(map_data, f, indent=2)

def save_commits_list(commits):
    """Guarda la lista de commits procesados en un archivo."""
    with open(COMMITS_LIST_FILE, "w") as f:
        json.dump(commits, f, indent=2)

def load_commits_list():
    """Carga la lista de commits desde un archivo."""
    if os.path.exists(COMMITS_LIST_FILE):
        with open(COMMITS_LIST_FILE, "r") as f:
            return json.load(f)
    return []

def extract_username_from_email(email):
    """Extrae el nombre de usuario de una dirección de correo electrónico."""
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
    """Infiere el nombre de usuario de GitHub basado en el nombre y correo del autor."""
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
    """Obtiene la lista de archivos afectados por un commit."""
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
            status = parts[0]

            if status.startswith('R'):

                if len(parts) >= 3:
                    result.append(parts[2])  
            else:

                if len(parts) >= 2:
                    result.append(parts[1])

        return result
    except Exception as e:
        log_message(f"Error al obtener archivos del commit {commit}: {str(e)}", "ERROR")

        return run(f"git show --pretty='' --name-only {ref}").splitlines()

def get_last_commit_affecting_file(file_path):
    """Obtiene el último commit que modificó un archivo."""
    cmd = f"git log -n 1 --pretty=format:'%H' -- {file_path}"
    if remote_name:
        cmd = f"git log -n 1 --pretty=format:'%H' {remote_name} -- {file_path}"
    return run(cmd).strip("'")

def ask_to_search_file(file_path):
    """Pregunta al usuario si desea buscar un archivo faltante."""
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
    """Busca un archivo en todas las ramas del remote especificado."""
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
    """Busca el commit que agregó un archivo, con métodos avanzados de búsqueda."""
    if remote_name:
        log_message(f"Buscando en remote '{remote_name}' el archivo '{file_path}'", "INFO")
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
            try:
                result = run(f"git log --diff-filter=A --format='%H' {ref} -- {file_path}")
                if result:
                    commits = result.splitlines()
                    if commits:
                        return commits[-1]
            except:
                pass

    result = run(f"git log --diff-filter=A --format='%H' -- {file_path}")
    if result:
        commits = result.splitlines()
        if commits:
            return commits[-1]

    result = run(f"git log --full-history --format='%H' -- {file_path} | tail -1")
    if result:
        return result

    basename = os.path.basename(file_path)
    log_message(f"Buscando archivos con nombre similar a '{basename}' en todo el historial", "INFO")

    search_cmd = f"git rev-list --all"
    if remote_name:
        search_cmd += f" {remote_name}"

    result = run(f"{search_cmd} | xargs -I{{}} git grep -l '{basename}' {{}} | head -1")
    if result:
        file_commit = run(f"git log -n 1 --pretty=format:'%H' {result}")
        if file_commit:
            return file_commit

    similar_files = find_similar_files(file_path)
    if similar_files:
        log_message(f"Encontrados {len(similar_files)} archivos similares a '{file_path}'", "INFO")
        for similar, score in similar_files:
            log_message(f"Archivo similar: '{similar}' (similitud: {score}%)", "INFO")
            result = run(f"git log --diff-filter=A --format='%H' -- {similar} | tail -1")
            if result:
                return result

    if remote_name:
        return search_file_in_remote(file_path)

    return None

def find_similar_files(file_path):
    """Encuentra archivos con nombres similares en el repositorio."""
    basename = os.path.basename(file_path)
    dirname = os.path.dirname(file_path)

    all_files = run("git ls-files").splitlines()

    similar_files = []

    for repo_file in all_files:
        repo_basename = os.path.basename(repo_file)
        repo_dirname = os.path.dirname(repo_file)

        name_similarity = calculate_similarity(basename, repo_basename)

        if dirname == repo_dirname:
            name_similarity += 20

        if name_similarity >= config["rename_detection_threshold"]:
            similar_files.append((repo_file, name_similarity))

    similar_files.sort(key=lambda x: x[1], reverse=True)
    return similar_files[:5]  

def calculate_similarity(str1, str2):
    """Calcula el porcentaje de similitud entre dos cadenas usando la distancia de Levenshtein."""
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
    """Encuentra la cadena completa de commits que afectan a un archivo."""
    remote_ref = f"{remote_name}/" if remote_name else ""

    creation_commit = find_commit_adding_file(file_path)
    if not creation_commit:
        log_message(f"No se pudo encontrar cuándo se agregó '{file_path}'", "WARNING")
        return []

    log_message(f"Commit de creación encontrado: {creation_commit[:8]}", "SUCCESS")

    try:
        target_ref = target_commit
        creation_ref = creation_commit

        if remote_name:
            remote_target = f"{remote_name}/{target_commit}"
            target_exists = run(f"git rev-parse --verify {remote_target}^{{commit}} 2>/dev/null", allow_fail=True)
            if target_exists:
                target_ref = remote_target

            remote_creation = f"{remote_name}/{creation_commit}"
            creation_exists = run(f"git rev-parse --verify {remote_creation}^{{commit}} 2>/dev/null", allow_fail=True)
            if creation_exists:
                creation_ref = remote_creation

        if target_commit:

            result = run(f"git log --format='%H' {creation_ref}~1..{target_ref} -- {file_path}", allow_fail=True)
            if result:
                commits = result.splitlines()
                if creation_commit not in commits:
                    commits = [creation_commit] + commits
                return commits

        log_message(f"Buscando todos los commits que afectan a '{file_path}'", "INFO")
        all_commits = run(f"git log --format='%H' --follow {remote_ref} -- {file_path}", allow_fail=True).splitlines()

        if not all_commits:
            return [creation_commit]

        return all_commits
    except Exception as e:
        log_message(f"Error buscando historia de commits: {str(e)}", "ERROR")

        return [creation_commit]

def find_file_history(file_path):
    """Obtiene la historia completa de un archivo con detalles."""
    cmd = f"git log --name-status --follow --format='%H %cr: %s' -- {file_path}"
    if remote_name:
        cmd = f"git log --name-status --follow --format='%H %cr: %s' {remote_name} -- {file_path}"

    return run(cmd)

def get_blame_and_grep_dependencies(commit, file, dep_cache=None):
    """Analiza un commit para encontrar dependencias basadas en el código."""
    cache_key = f"{commit}:{file}"
    if dep_cache is not None and cache_key in dep_cache:
        return dep_cache[cache_key]

    remote_ref = f"{remote_name}/" if remote_name else ""
    commit_ref = f"{remote_ref}{commit}" if remote_name else commit

    try:
        file_content = run(f"git show {commit}:{file}")
    except Exception:
        return []

    if not file_content:
        return []

    lines = file_content.splitlines()

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
            if line and len(line.split()) == 4 and len(line.split()[0]) == 40:
                blame_commits.add(line.split()[0])

    suspects = set(blame_commits)

    for i, line in enumerate(lines):
        line = line.strip()
        if not line or line.startswith("//") or line.startswith("#"):
            continue

        if "(" in line and ")" in line:
            pre_paren = line.split("(")[0].strip()
            if pre_paren:
                pre_split = pre_paren.split()
                if pre_split:

                    fname = pre_split[-1]
                    if fname.isidentifier():

                        grep_cmd = f"git log -S'{fname}' --pretty=format:'%H' -- {file}"
                        if remote_name:
                            grep_cmd = f"git log -S'{fname}' --pretty=format:'%H' {remote_name} -- {file}"

                        grep_result = run(grep_cmd)
                        if grep_result:
                            suspects.update(grep_result.splitlines())

    includes = extract_includes(file_content)
    for include_file in includes:
        try:

            include_commit = find_commit_adding_file(include_file)
            if include_commit:
                suspects.add(include_commit)
        except Exception:
            pass

    suspects = list(suspects)
    if dep_cache is not None:
        dep_cache[cache_key] = suspects
    return suspects

def extract_includes(file_content):
    """Extrae paths de archivos incluidos/importados en el código fuente."""
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
    """Obtiene información detallada de un commit en formato legible."""
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
    """Agrega un commit a las listas de seguimiento sin duplicar."""
    if commit not in final_commits:
        final_commits.append(commit)
    if commit not in cherry_pick_queue:
        cherry_pick_queue.append(commit)

def count_unique_pending_commits():
    """Cuenta el número de commits únicos pendientes de aplicar."""
    unique_commits = set(final_commits + cherry_pick_queue)
    if initial_commit and initial_commit not in unique_commits:
        unique_commits.add(initial_commit)
    return len(unique_commits)

def show_progress(current, total, message="Procesando"):
    """Muestra una barra de progreso en la consola."""
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
    """Analiza un commit para detectar dependencias y problemas potenciales."""
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
                log_message(f"Archivo '{actual_file}' ya fue procesado anteriormente, omitiendo análisis repetido", "INFO")
                continue

            processed_missing_files.add(file_commit_key)

            if not ask_to_search_file(actual_file):
                log_message(f"Ignorando archivo faltante: {actual_file}", "WARNING")
                continue

            log_message(f"Buscando commits relacionados con '{actual_file}'", "INFO")
            adding_commit = find_commit_adding_file(file)

            if adding_commit:
                context = get_commit_context(adding_commit)
                print(Fore.MAGENTA + f"El archivo fue agregado originalmente en:\n  {context}")

                commit_chain = find_commit_history_chain(file, commit)

                if commit_chain and len(commit_chain) > 1:
                    print(Fore.MAGENTA + f"Se encontraron {len(commit_chain)} commits que modifican este archivo:")

                    max_display = min(config["max_commits_display"], len(commit_chain))
                    for i, c in enumerate(commit_chain[:max_display], 1):
                        c_context = get_commit_context(c)
                        print(Fore.CYAN + f"  {i}. {c_context}")

                    if len(commit_chain) > max_display:
                        print(Fore.CYAN + f"     ... y {len(commit_chain)-max_display} más.")

                    total_pending = count_unique_pending_commits()
                    options = [
                        f"Agregar toda la cadena de {len(commit_chain)} commits relacionados con este archivo",
                        f"Agregar solo el commit que creó el archivo ({adding_commit[:8]})",
                        f"Continuar con el cherry-pick ({total_pending})"
                    ]
                    option = select_option(options)

                    if option.startswith("Agregar toda"):
                        for c in commit_chain:
                            add_commit_once(c)
                            print(Fore.BLUE + f"Se agregó {c} a la lista de commits a aplicar.")

                        created_files.add(actual_file)

                        if stop_analysis:
                            return
                    elif option.startswith("Agregar solo"):
                        add_commit_once(adding_commit)
                        print(Fore.BLUE + f"Se agregó {adding_commit} a la lista de commits a aplicar.")

                        created_files.add(actual_file)

                        if stop_analysis:
                            return
                    else:
                        stop_analysis = True
                        return
                else:

                    total_pending = count_unique_pending_commits()
                    options = [
                        f"Agregar commit que creó el archivo {adding_commit[:8]}",
                        f"Continuar con el cherry-pick ({total_pending})"
                    ]
                    option = select_option(options)

                    if option.startswith("Agregar"):
                        add_commit_once(adding_commit)
                        print(Fore.BLUE + f"Se agregó {adding_commit} a la lista de commits a aplicar.")

                        created_files.add(actual_file)

                        if stop_analysis:
                            return
                    elif option.startswith("Continuar"):
                        stop_analysis = True
                        return
            else:

                print(Fore.RED + f"No se pudo encontrar ningún commit que haya creado '{actual_file}'.")
                total_pending = count_unique_pending_commits()
                options = [
                    f"Especificar un archivo local equivalente",
                    f"Continuar con el cherry-pick ({total_pending})",
                    f"Cancelar análisis"
                ]
                option = select_option(options)

                if option.startswith("Especificar"):
                    local_name = input(f"Archivo local para '{actual_file}': ").strip()
                    file_renames[actual_file] = local_name

                    save_file_renames()
                elif option.startswith("Cancelar"):
                    stop_analysis = True
                    return

        last_commit = get_last_commit_affecting_file(actual_file)
        if last_commit and last_commit != commit and last_commit not in analyzed_commits and last_commit not in applied_commits:
            context = get_commit_context(last_commit)
            print(Fore.MAGENTA + f"\n'{file}' fue modificado antes en:\n  {context}")

            total_pending = count_unique_pending_commits()
            options = [
                f"Agregar commit faltante a la lista de commits a aplicar {last_commit}",
                f"Continuar con el cherry-pick ({total_pending})"
            ]
            option = select_option(options)

            if option.startswith("Agregar"):
                add_commit_once(last_commit)
                print(Fore.BLUE + f"Se agregó {last_commit} a la lista de commits a aplicar.")
                analyze_commit(last_commit, dep_cache)
                if stop_analysis:
                    return
            elif option.startswith("Continuar"):
                stop_analysis = True
                return

        if stop_analysis:
            return

        dependencies = get_blame_and_grep_dependencies(commit, actual_file, dep_cache)

        relevant_deps = [dep for dep in dependencies 
                        if dep not in analyzed_commits 
                        and dep not in applied_commits
                        and dep != commit]

        for dep_commit in relevant_deps:
            context = get_commit_context(dep_commit)
            print(Fore.MAGENTA + f"\nDependencia detectada en {actual_file}:\n  {context}")

            if config["auto_add_dependencies"]:
                add_commit_once(dep_commit)
                print(Fore.BLUE + f"Se agregó automáticamente {dep_commit} a la lista de commits.")
                continue

            total_pending = count_unique_pending_commits()
            options = [
                f"Agregar commit faltante a la lista de commits a aplicar {dep_commit}",
                f"Continuar con el cherry-pick ({total_pending})"
            ]
            choice = select_option(options)

            if choice.startswith("Agregar"):
                add_commit_once(dep_commit)
                print(Fore.BLUE + f"Se agregó {dep_commit} a la lista de commits a aplicar.")
                analyze_commit(dep_commit, dep_cache)
                if stop_analysis:
                    return
            elif choice.startswith("Continuar"):
                stop_analysis = True
                return

            if stop_analysis:
                return

def save_file_renames():
    """Guarda el mapeo de archivos renombrados para futuros usos."""
    with open(".smart_cherry_pick_renames.json", "w") as f:
        json.dump(file_renames, f, indent=2)

def load_file_renames():
    """Carga el mapeo de archivos renombrados de usos anteriores."""
    if os.path.exists(".smart_cherry_pick_renames.json"):
        with open(".smart_cherry_pick_renames.json", "r") as f:
            return json.load(f)
    return {}

def process_commit(commit, dep_cache):
    """Procesa un commit, preguntando primero si se debe analizar, aplicar directamente o editar antes de aplicarlo."""
    print(Fore.GREEN + f"\nProcesando commit: {get_commit_context(commit)}")

    if auto_mode:
        log_message("Modo automático: analizando commit para buscar dependencias", "INFO")
        analyze_commit(commit, dep_cache)
        return

    options = [
        "Analizar commit para buscar dependencias",
        "Aplicar cherry-pick directamente sin análisis",
        "Editar el contenido y mensaje del commit antes de aplicarlo"
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

        commit_ref = commit
        if remote_name:
            remote_ref = f"{remote_name}/{commit}"
            test_exists = run(f"git rev-parse --verify {remote_ref}^{{commit}} 2>/dev/null", allow_fail=True)
            if test_exists:
                commit_ref = remote_ref

        result = subprocess.run(f"git cherry-pick --empty=drop {commit_ref}", shell=True)

        if result.returncode == 0:
            applied_commits.add(commit)
            final_commits.append(commit)
            print(Fore.GREEN + f"Commit {commit} aplicado exitosamente.")
        else:
            log_message(f"Error al aplicar cherry-pick directo para {commit}", "WARNING")
            handle_cherry_pick_error(commit)
    elif choice.startswith("Editar"):
        edit_commit_before_applying(commit)

def edit_commit_before_applying(commit):
    """
    Permite al usuario editar manualmente el contenido y el mensaje del commit antes de aplicarlo.
    Se realiza un cherry-pick en modo no-commit (-n) para que los cambios se apliquen y se
    registren en el índice. Luego se abre el editor configurado para que el usuario modifique
    los archivos modificados y, a continuación, se edita el mensaje original del commit.
    Finalmente se agregan los cambios y se crea el commit.
    """
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
    """Pregunta al usuario si desea proceder con los cherry-picks."""

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
    """Aplica los cherry-picks en el orden correcto."""

    for commit in initial_commits:
        if commit not in final_commits and commit not in skipped_commits:
            final_commits.append(commit)

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
    success_count = 0
    fail_count = 0

    for idx, commit in enumerate(commit_list):
        show_progress(idx + 1, total_commits, "Aplicando commits")

        if commit in applied_commits:
            log_message(f"Commit {commit} ya fue aplicado previamente. Saltando.", "INFO")
            success_count += 1
            continue

        if commit in skipped_commits:
            log_message(f"Commit {commit} está en la lista de commits a omitir. Saltando.", "INFO")
            continue

        log_message(f"Aplicando cherry-pick para {commit}", "INFO")

        commit_ref = commit
        if remote_name:
            remote_ref = f"{remote_name}/{commit}"
            test_exists = run(f"git rev-parse --verify {remote_ref}^{{commit}} 2>/dev/null", allow_fail=True)
            if test_exists:
                commit_ref = remote_ref

        print(Fore.GREEN + f"\n[{idx+1}/{total_commits}] Aplicando cherry-pick --empty=drop {commit}...")
        result = subprocess.run(f"git cherry-pick --empty=drop {commit_ref}", shell=True)

        if result.returncode != 0:
            fail_count += 1
            handle_cherry_pick_error(commit)
            continue

        applied_commits.add(commit)
        success_count += 1
        log_message(f"Commit {commit} aplicado exitosamente.", "SUCCESS")

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
    """Extrae nombres de archivos que no existen en el índice de la salida de error."""
    not_exist_files = []
    pattern = r"error: ([^:]+): does not exist in index"
    matches = re.findall(pattern, error_output)

    for match in matches:
        not_exist_files.append(match.strip())

    return not_exist_files

def handle_cherry_pick_error(commit):
    """Maneja errores durante el proceso de cherry-pick."""
    log_message(f"Error al aplicar el commit {commit}. Analizando conflictos...", "ERROR")

    error_output = run("git status 2>&1")
    not_exist_files = parse_not_existing_files(error_output)
    failed_files = run("git diff --name-only --diff-filter=U").splitlines()

    if not_exist_files:
        print(Fore.YELLOW + f"Se encontraron {len(not_exist_files)} archivos que no existen en el índice:")
        for f in not_exist_files:
            print(Fore.YELLOW + f" - {f}")

        run("git cherry-pick --abort")

        for missing_file in not_exist_files:

            if not ask_to_search_file(missing_file):
                log_message(f"Ignorando archivo faltante: {missing_file}", "WARNING")
                continue

            adding_commit = find_commit_adding_file(missing_file)

            if adding_commit:
                context = get_commit_context(adding_commit)
                print(Fore.CYAN + f"\nEl archivo '{missing_file}' fue agregado originalmente en:\n  {context}")

                handle_missing_file(missing_file, adding_commit, commit)
            else:
                log_message(f"No se pudo encontrar el commit que agregó '{missing_file}'", "WARNING")

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
        else:
            log_message(f"Todavía hay problemas al aplicar el commit. Puedes intentar manualmente.", "ERROR")

    elif failed_files:
        print(Fore.YELLOW + f"Archivos con conflictos detectados ({len(failed_files)}):")
        for f in failed_files:
            print(Fore.YELLOW + f" - {f}")

        options = [
            "Resolver conflictos manualmente", 
            "Intentar manejo automático de archivos renombrados", 
            "Abortar cherry-pick"
        ]
        choice = select_option(options)

        if choice.startswith("Resolver"):

            resume_cherry_pick(failed_files)
        elif choice.startswith("Intentar"):

            run("git cherry-pick --abort")
            handled = ask_file_renames_from_errors(failed_files)
            if handled:
                apply_patch_with_rename_handling(commit)
            else:
                log_message("No se pudo manejar los renombres. Saltando commit.", "ERROR")
        else:

            run("git cherry-pick --abort")
            log_message(f"Cherry-pick abortado para el commit {commit}.", "WARNING")
    else:

        run("git cherry-pick --abort")
        log_message(f"Cherry-pick fallido pero no se detectaron conflictos. Saltando commit.", "ERROR")

def get_current_branch():
    return subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True, text=True, check=False
    ).stdout.strip()

def get_file_content_at_commit(file_path, commit, branch_name):
    """Obtiene el contenido de un archivo en un commit específico."""

    content = run(f"git show {commit}:{file_path}", allow_fail=True)
    branch_name = get_current_branch()
    print(f"Rama actual: {branch_name}")

    if content:
       return content  

    if remote_name:
        if verbose_mode:
            log_message(f"Intentando obtener {file_path} de {commit} vía remote {remote_name}", "DEBUG")

        run(f"git fetch {remote_name} {commit}", allow_fail=True)

        content = run(f"git show {commit}:{file_path}", allow_fail=True)
        if content:
           return content  

        log_message(f"No se pudo obtener contenido de {file_path} en commit {commit}", "WARNING")

    return None

def handle_missing_file(missing_file, adding_commit, current_commit):
    """Maneja archivos faltantes ofreciendo opciones al usuario."""
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

            file_content = get_file_content_at_commit(missing_file, current_commit)
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
    Permite editar manualmente las rutas de TODOS los archivos conflictivos.
    Se muestra un menú en el que se listan individualmente los archivos conflictivos (mostrando, de ser el caso,
    la ruta corregida previamente) y se ofrece una opción adicional (opción 4) para continuar con las rutas que se han
    modificado. La función retorna True cuando se selecciona la opción de continuar.
    """
    while True:
        print(Fore.RED + "\nCherry-pick fallido. Posibles errores por renombre de archivos:")
        # Se muestran cada uno de los archivos conflictivos
        for i, archivo in enumerate(failed_files, 1):
            ruta_actual = file_renames.get(archivo, archivo)
            print(Fore.YELLOW + f"{i}. {ruta_actual}")
        print(Fore.YELLOW + "4. Continuar con las rutas modificadas")
        
        opcion = input("Selecciona una opción: ").strip()
        if opcion == "4":
            # El usuario confirma que ya ha editado (o desea continuar con) todas las rutas
            return True
        try:
            idx = int(opcion)
        except ValueError:
            print(Fore.RED + "Opción inválida. Intenta de nuevo.")
            continue
        if 1 <= idx <= len(failed_files):
            archivo = failed_files[idx - 1]
            ruta_actual = file_renames.get(archivo, archivo)
            nueva_ruta = input(f"Ingrese la ruta local correcta para '{ruta_actual}': ").strip()
            if nueva_ruta:
                file_renames[archivo] = nueva_ruta
                log_message(f"Renombramiento editado: {archivo} -> {nueva_ruta}", "INFO")
            else:
                print(Fore.RED + "No se ingresó ninguna ruta. Se mantiene la ruta actual.")
        else:
            print(Fore.RED + "Opción inválida. Intenta de nuevo.")


def apply_patch_with_rename_handling(commit):
    """
    Genera el parche correspondiente al commit y lo aplica.
    Si al aplicarlo se presentan errores en los archivos por renombrado,
    se recopila la lista completa de archivos conflictivos y se llama a
    ask_file_renames_from_errors para que el usuario edite la ruta de TODOS ellos.
    Una vez que el usuario selecciona la opción "Continuar con las rutas modificadas",
    se regenera el parche incorporando todas las correcciones registradas y se reintenta la aplicación.
    """
    commit_ref = commit
    if remote_name:
        remote_ref = f"{remote_name}/{commit}"
        test_exists = run(f"git rev-parse --verify {remote_ref}^{{commit}} 2>/dev/null", allow_fail=True)
        if test_exists:
            commit_ref = remote_ref

    print(Fore.GREEN + f"\nAplicando cherry-pick -n {commit}...")
    patch = run(f"git format-patch -1 {commit_ref} --stdout")
    if not patch.strip():
        log_message("El commit no tiene cambios. Saltando.", "WARNING")
        return False

    # Se reemplazan en el parche las rutas que ya se hayan corregido previamente
    for origen, destino in file_renames.items():
        patch = patch.replace(origen, destino)

    with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmpfile:
        tmpfile.write(patch)
        tmpfile_path = tmpfile.name

    result = subprocess.run(f"git apply --index {tmpfile_path}", shell=True)
    if result.returncode != 0:
        # Se obtiene la salida que indica los archivos con error en la aplicación del parche
        failed_output = run(f"git apply --check {tmpfile_path} 2>&1")
        failed_files = []
        for line in failed_output.splitlines():
            if "patch does not apply" in line or "patch failed" in line:
                partes = line.split(":")
                if len(partes) >= 2:
                    archivo_error = partes[0].replace("error", "").strip()
                    # Si vienen varias rutas separadas por ':' (por ejemplo, "ruta1:ruta2") se pueden separar:
                    subarchivos = archivo_error.split(":")
                    for sub in subarchivos:
                        sub = sub.strip()
                        if sub and sub not in failed_files:
                            failed_files.append(sub)
        os.unlink(tmpfile_path)
        if failed_files:
            print(Fore.RED + "\nSe detectaron los siguientes errores:")
            for f in failed_files:
                print(Fore.CYAN + f" - {f}")
            # Se llama a la función para que el usuario corrija manualmente TODOS los archivos conflictivos.
            if ask_file_renames_from_errors(failed_files):
                # Regenera el parche incorporando las rutas corregidas
                patch = run(f"git format-patch -1 {commit_ref} --stdout")
                for origen, destino in file_renames.items():
                    patch = patch.replace(origen, destino)
                with tempfile.NamedTemporaryFile(mode="w+", delete=False) as tmpfile2:
                    tmpfile2.write(patch)
                    tmpfile_path2 = tmpfile2.name
                result = subprocess.run(f"git apply --index {tmpfile_path2}", shell=True)
                os.unlink(tmpfile_path2)
                if result.returncode != 0:
                    log_message("Error al aplicar el parche después de corregir las rutas.", "ERROR")
                    return False
            else:
                log_message("No se pudieron resolver los conflictos de renombrado.", "ERROR")
                return False
        else:
            log_message("No se pudieron identificar archivos conflictivos.", "ERROR")
            return False
    else:
        os.unlink(tmpfile_path)

    # Una vez aplicado el parche, se procede a crear el commit.
    result = subprocess.run("git diff --cached --quiet", shell=True)
    if result.returncode != 0:
        message = run(f"git log -1 --pretty=format:%B {commit_ref}").strip()
        try:
            with tempfile.NamedTemporaryFile(mode="w+", delete=False) as msg_file:
                msg_file.write(message)
                msg_file_path = msg_file.name
            subprocess.run(f"git commit -F {msg_file_path}", shell=True)
            os.unlink(msg_file_path)
        except Exception as e:
            log_message(f"Error al crear commit: {str(e)}", "ERROR")
            subprocess.run(f"git commit -m \"Cherry-pick {commit}\"", shell=True)
    log_message(f"Commit {commit} aplicado exitosamente con manejo de renombres.", "SUCCESS")
    return True

def handle_cherry_pick_error(commit):
    """Maneja errores durante el proceso de cherry-pick."""
    log_message(f"Error al aplicar el commit {commit}. Analizando conflictos...", "ERROR")

    error_output = run("git status 2>&1")
    not_exist_files = parse_not_existing_files(error_output)
    failed_files = run("git diff --name-only --diff-filter=U").splitlines()

    if not_exist_files:
        print(Fore.YELLOW + f"Se encontraron {len(not_exist_files)} archivos que no existen en el índice:")
        for f in not_exist_files:
            print(Fore.YELLOW + f" - {f}")

        run("git cherry-pick --abort")

        for missing_file in not_exist_files:

            if not ask_to_search_file(missing_file):
                log_message(f"Ignorando archivo faltante: {missing_file}", "WARNING")
                continue

            adding_commit = find_commit_adding_file(missing_file)

            if adding_commit:
                context = get_commit_context(adding_commit)
                print(Fore.CYAN + f"\nEl archivo '{missing_file}' fue agregado originalmente en:\n  {context}")

                handle_missing_file(missing_file, adding_commit, commit)
            else:
                log_message(f"No se pudo encontrar el commit que agregó '{missing_file}'", "WARNING")

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
        else:
            log_message(f"Todavía hay problemas al aplicar el commit. Puedes intentar manualmente.", "ERROR")

    elif failed_files:
        print(Fore.YELLOW + f"Archivos con conflictos detectados ({len(failed_files)}):")
        for f in failed_files:
            print(Fore.YELLOW + f" - {f}")

        options = [
            "Resolver conflictos manualmente", 
            "Intentar manejo automático de archivos renombrados", 
            "Abortar cherry-pick"
        ]
        choice = select_option(options)

        if choice.startswith("Resolver"):

            resume_cherry_pick(failed_files)
        elif choice.startswith("Intentar"):

            run("git cherry-pick --abort")
            handled = ask_file_renames_from_errors(failed_files)
            if handled:
                apply_patch_with_rename_handling(commit)
            else:
                log_message("No se pudo manejar los renombres. Saltando commit.", "ERROR")
        else:

            run("git cherry-pick --abort")
            log_message(f"Cherry-pick abortado para el commit {commit}.", "WARNING")
    else:

        run("git cherry-pick --abort")
        log_message(f"Cherry-pick fallido pero no se detectaron conflictos. Saltando commit.", "ERROR")

def get_preferred_editor():

    if config["default_editor"]:
        return config["default_editor"]

    if os.path.exists("/data/data/com.termux/files/usr/bin/nvim"):
        return "nvim"
    elif os.path.exists("/data/data/com.termux/files/usr/bin/vim"):
        return "vim"

    return os.environ.get("EDITOR", "vi")

def resume_cherry_pick(conflicted_files):
    log_message("Resolviendo conflictos del cherry-pick...", "INFO")

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

    if result.returncode == 0:
        log_message("Cherry-pick continuado exitosamente.", "SUCCESS")
        return True
    else:
        log_message("Error al continuar cherry-pick.", "ERROR")
        log_message("Puedes intentar manualmente con:", "INFO")
        log_message("1. git add <archivos_resueltos>", "INFO")
        log_message("2. git cherry-pick --continue", "INFO")
        return False

def list_history():
    commits = load_history()
    print(Fore.CYAN + "\nCommits aplicados previamente:")
    for c in commits:
        print(Fore.CYAN + f" - {get_commit_context(c)}")

def get_commit_range(start_commit, end_commit):
    if remote_name:
        run(f"git fetch {remote_name}")

    start_ref = start_commit
    end_ref = end_commit

    if remote_name:
        remote_start = f"{remote_name}/{start_commit}"
        remote_start_exists = run(f"git rev-parse --verify {remote_start}^{{commit}} 2>/dev/null", allow_fail=True)
        if remote_start_exists:
            start_ref = remote_start

        remote_end = f"{remote_name}/{end_commit}"
        remote_end_exists = run(f"git rev-parse --verify {remote_end}^{{commit}} 2>/dev/null", allow_fail=True)
        if remote_end_exists:
            end_ref = remote_end

    start_exists = run(f"git rev-parse --verify {start_ref}^{{commit}} 2>/dev/null")
    if not start_exists:
        log_message(f"Error: El commit inicial {start_commit} no existe.", "ERROR")
        sys.exit(1)

    end_exists = run(f"git rev-parse --verify {end_ref}^{{commit}} 2>/dev/null")
    if not end_exists:
        log_message(f"Error: El commit final {end_commit} no existe.", "ERROR")
        sys.exit(1)

    commits = run(f"git rev-list --reverse {start_ref}^..{end_ref}").splitlines()

    if not commits:
        log_message("No se encontraron commits en el rango especificado.", "ERROR")
        sys.exit(1)

    return commits

def show_help():
    help_text = f"""
{Fore.GREEN}Smart Cherry Pick - Herramienta para aplicar commits de manera inteligente

{Fore.CYAN}Uso:
  python3 smart_cherry_pick.py <commit_hash> [commit_hash2 ... commit_hashN] [opciones]
  python3 smart_cherry_pick.py --range-commits <start_commit> <end_commit> [opciones]
  python3 smart_cherry_pick.py --help

{Fore.CYAN}Argumentos:
  <commit_hash>          Uno o más hashes de commits para aplicar
  --range-commits        Especifica un rango de commits para aplicar
    start_commit         Commit inicial (más antiguo) del rango
    end_commit           Commit final (más reciente) del rango

{Fore.CYAN}Opciones:
  --remote REMOTE_NAME   Nombre del remote de Git (ej: origin, upstream, rem2)
  --skip-commit COMMITS  Lista de commits a omitir cuando se usa --range-commits
  --auto                 Modo automático (usa opciones por defecto sin preguntar)
  --verbose              Modo verboso (muestra más información)
  --dry-run              Simula el proceso sin aplicar cambios
  --apply-saved          Aplica los commits guardados previamente
  --config KEY=VALUE     Establece opciones de configuración
  --help                 Muestra este mensaje de ayuda

{Fore.CYAN}Ejemplos:
  python3 smart_cherry_pick.py abc1234
  python3 smart_cherry_pick.py abc1234 def5678 --remote origin
  python3 smart_cherry_pick.py --range-commits abc1234 def5678 --remote rem2
  python3 smart_cherry_pick.py --range-commits abc1234 def5678 --skip-commit ghi9012 jkl3456
  python3 smart_cherry_pick.py --auto --range-commits abc1234 def5678
    """
    print(help_text)

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

    try:
        args = parser.parse_args()
    except SystemExit:
        show_help()
        return

    if args.help or (not args.commits and not args.range_commits and not args.apply_saved):
        show_help()
        return

    if args.auto:
        auto_mode = True
    if args.verbose:
        verbose_mode = True
    if args.dry_run:
        dry_run = True
        print(Fore.YELLOW + "Modo simulación activado. No se aplicarán cambios reales.")

    load_config()
    if args.config:
        update_config_from_args(args.config)

    if not os.path.exists(LOG_FILE):
        open(LOG_FILE, "w").close()

    log_message("Iniciando Smart Cherry Pick", "INFO")

    if args.remote:
        remote_name = args.remote
        log_message(f"Usando remote '{remote_name}' para buscar commits y archivos.", "INFO")
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

    save_history(applied_commits)
    save_dep_cache(dep_cache)
    save_author_map(author_map)

    elapsed_time = int(time.time() - start_time)
    log_message(f"Proceso completado en {elapsed_time} segundos.", "SUCCESS")
    print(Fore.GREEN + f"\nProceso completado en {elapsed_time} segundos.")

if __name__ == "__main__":
    main()