# Smart Cherry Pick

Herramienta avanzada para realizar cherry-picks inteligentes en repositorios Git, con análisis de dependencias, resolución automática de conflictos y manejo eficiente de commits.

## Características

- Análisis automático de dependencias entre commits
- Manejo inteligente de archivos inexistentes o renombrados
- Resolución automática de conflictos (modo auto)
- Estadísticas de rendimiento y éxito
- Soporte para remotes Git, incluso los no configurados
- Paginación y optimización para proyectos grandes
- Resumen detallado de las operaciones realizadas

## Uso

```bash
python3 smart-chery-pick.py <commit_hash> [commit_hash2 ... commit_hashN] [opciones]
```

### Ejemplos

```bash
# Aplicar un único commit
python3 smart-chery-pick.py abc1234

# Aplicar varios commits específicos desde un remote
python3 smart-chery-pick.py abc1234 def5678 --remote origin

# Aplicar un rango de commits
python3 smart-chery-pick.py --range-commits abc1234 def5678

# Aplicar automáticamente commits de un rango
python3 smart-chery-pick.py --auto --range-commits abc1234 def5678
```

### Opciones

- `--range-commits START END`: Aplica todos los commits entre START y END
- `--skip-commit HASH1 HASH2`: Omite commits específicos
- `--remote REMOTE_NAME`: Utiliza un remote específico para buscar commits
- `--auto`: Modo automático (utiliza opciones por defecto sin preguntar)
- `--verbose`: Modo detallado (muestra más información en consola)
- `--dry-run`: Simula la ejecución sin aplicar cambios reales
- `--apply-saved`: Aplica los commits guardados en una sesión anterior
- `--config KEY=VALUE`: Establece opciones de configuración
- `--no-stats`: No registrar estadísticas de rendimiento

## Estadísticas y Rendimiento

La herramienta registra estadísticas detalladas en formato CSV para analizar:

- Tasa de éxito en cherry-picks
- Tiempo de ejecución por commit
- Número de conflictos y método de resolución
- Rendimiento en proyectos grandes

Para ver las estadísticas, consulta el archivo `.smart_cherry_pick_stats.csv` generado.

## Configuración Avanzada

El archivo `.smart_cherry_pick_config.json` permite personalizar:

- `max_commits_display`: Número máximo de commits para mostrar (defecto: 5)
- `max_search_depth`: Profundidad máxima para buscar dependencias (defecto: 100)
- `rename_detection_threshold`: Umbral para detección de archivos renombrados (defecto: 50)
- `auto_add_dependencies`: Agregar automáticamente dependencias encontradas (defecto: false)
- `record_stats`: Registrar estadísticas de rendimiento (defecto: true)

## Licencia

Este proyecto es software libre y puede ser distribuido bajo los términos de la licencia GNU GPL v3.