import argparse
import asyncio
import os
import re
import threading
import types
from collections import deque
from typing import (
    override,
    TYPE_CHECKING,
    Generator,
    Any,
    get_args,
    cast,
    get_origin,
    Union,
    Optional,
    Callable,
)

from diskcache import Cache
from dotenv import find_dotenv
from pydantic import BaseModel
from sqlalchemy import func, select
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.widget import Widget
from textual.widgets import (
    Tabs,
    Label,
    ContentSwitcher,
    Tab,
    Log,
    OptionList,
    TextArea,
    RichLog,
    Input,
    Button,
)
from textual.widgets._option_list import Option

from crawler.engine import Crawler, QueueRecharger
from src.common.paths import config_files_folder_path
from src.configs.crawler_config import CrawlerConfig
from src.crawler.bootstrap import launch_crawler
from src.crawler.log_levels import LoggingLevels
from src.database.model import CrawledURL, WaitingURL
from src.database.session import create_db_engine

if TYPE_CHECKING:
    from loguru import Message

# Définition ultra-spécifique des scalaires de configuration (sans aucun Any)
type ConfigsTypes = type[int] | type[float] | type[str] | type[bool] | type[None] | types.UnionType
type ConfigsTypesTree = ConfigsTypes | type[list[Any]] | types.GenericAlias | dict[str, ConfigsTypesTree]

class CallbackButton(Button):
    def __init__(self, label: str, callback: Callable[[], None], *args, **kwargs):
        super().__init__(label, *args, **kwargs)
        self.callback = callback

    def on_button_pressed(self, _event: Button.Pressed):
        self.callback()


class ToggleButtons(Container):
    def __init__(self, pause_button: Button, resume_button: Button, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.pause_button = pause_button
        self.resume_button = resume_button

    def compose(self) -> ComposeResult:
        self.pause_button.disabled = False
        self.resume_button.disabled = True

        yield self.pause_button
        yield self.resume_button

    def on_button_pressed(self, event: Button.Pressed) -> None:
        self.set_state(event.button == self.pause_button)

    def set_state(self, state: bool) -> None:
        self.pause_button.disabled = state
        self.resume_button.disabled = not state


class DashboardPage(Container):
    def __init__(self, crawlers: list[dict[Crawler, tuple[threading.Event, threading.Event]]], queue_recharger: dict[QueueRecharger, tuple[threading.Event, threading.Event]], cache: Cache | None = None, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.cache = cache
        self._engine = create_db_engine()
        
        self.crawlers = crawlers
        self.queue_recharger = queue_recharger

    def compose(self) -> ComposeResult:
        yield Label("", id="domains")
        yield Label("", id="crawled_pages")
        yield Label("", id="waiting_pages")

        def yield_thread_buttons(crawler: dict[Crawler | QueueRecharger, tuple[threading.Event, threading.Event]]) -> Generator[Widget, None, None]:
            with Horizontal():
                yield Label(list(crawler.keys())[0].name)
                yield ToggleButtons(
                    CallbackButton("Pause", callback=list(crawler.values())[0][1].set),
                    CallbackButton("Resume", callback=list(crawler.values())[0][1].clear, disabled=True)
                )
                yield CallbackButton("Stop", callback=list(crawler.values())[0][0].set)

        yield from yield_thread_buttons(self.queue_recharger)
        for crawler in self.crawlers:
            with Horizontal():
                yield from yield_thread_buttons(crawler)

    def on_mount(self):
        self.set_interval(5, self.update_content)

    def update_content(self):
        # noinspection PyBroadException
        try:
            with self._engine.connect() as conn:
                num_crawled_urls = conn.scalar(select(func.count()).select_from(CrawledURL))
                num_urls_in_waiting_list = conn.scalar(select(func.count()).select_from(WaitingURL))

            num_domains = len(self.cache) if self.cache else 0
            self.query_one("#domains", Label).update(f"Number of domains visited: {num_domains}")
            self.query_one("#crawled_pages", Label).update(f"Number of pages visited: {num_crawled_urls}")
            self.query_one("#waiting_pages", Label).update(f"Number of waiting pages: {num_urls_in_waiting_list}")
        except Exception:
            self.query_one("#domains", Label).update("Initializing...")
            self.query_one("#crawled_pages", Label).update("Initializing...")
            self.query_one("#waiting_pages", Label).update("Initializing...")


class LogsPage(Log):
    BINDINGS = [
        Binding("escape", "back_to_home", "Retour"),
        Binding("backspace", "back_to_home", "Retour"),
        Binding("+", "increase_level", "Niveau Sup. (+)", key_display="+"),
        Binding("-", "decrease_level", "Niveau Inf. (-)", key_display="-"),
    ]

    def __init__(self, *args, **kwargs):
        kwargs["max_lines"] = 1000
        super().__init__(*args, **kwargs)

        self.log_history: deque[tuple[int, str, str]] = deque(maxlen=1000)
        self.levels = [5, 10, 20, 25, 30, 40, 50, 60]
        self._crawler_name = "Crawler-all"
        self.current_level_index = 2

        self.buffer_lock = threading.Lock()
        self.log_buffer: list[tuple[int, str, str]] = []

    @property
    def crawler_name(self) -> str:
        return self._crawler_name

    @crawler_name.setter
    def crawler_name(self, crawler_name: str) -> None:
        self._crawler_name = crawler_name
        self._refresh_logs()

    def on_mount(self) -> None:
        self.set_interval(0.2, self.pull_logs)

    def action_back_to_home(self) -> None:
        home: LogsHomePage = self.app.logs_home_page
        home.query_one(ContentSwitcher).current = "option_list"
        home.focus()

    def pull_logs(self):
        with self.buffer_lock:
            if not self.log_buffer:
                return
            batch = self.log_buffer
            self.log_buffer = []

        threshold = self.current_level_no
        to_write = []
        for level_no, crawler_name, raw_msg in batch:
            self.log_history.append((level_no, crawler_name, raw_msg))
            if level_no >= threshold and (crawler_name == self.crawler_name or self.crawler_name == "Crawler-all"):
                to_write.append(raw_msg)

        if to_write:
            self.write_lines(to_write)

    @property
    def current_level_no(self) -> int:
        return self.levels[self.current_level_index]

    @property
    def current_level_name(self) -> LoggingLevels:
        levels_map = {
            5: LoggingLevels.TRACE,
            10: LoggingLevels.DEBUG,
            20: LoggingLevels.INFO,
            25: LoggingLevels.SUCCESS,
            30: LoggingLevels.WARNING,
            40: LoggingLevels.ERROR,
            50: LoggingLevels.CRITICAL,
            60: LoggingLevels.FATAL,
        }
        return levels_map.get(self.current_level_no, LoggingLevels.INFO)

    def action_increase_level(self):
        if self.current_level_index < len(self.levels) - 1:
            self.current_level_index += 1
            self._refresh_logs()
            self.app.notify(f"Niveau minimum : {self.current_level_name}", title="Log Level")

    def action_decrease_level(self):
        if self.current_level_index > 0:
            self.current_level_index -= 1
            self._refresh_logs()
            self.app.notify(f"Niveau minimum : {self.current_level_name}", title="Log Level")

    def _refresh_logs(self):
        self.clear()
        threshold = self.current_level_no

        to_write = [raw_msg for lvl, crawler_name, raw_msg in self.log_history if lvl >= threshold and (crawler_name == self.crawler_name or self.crawler_name == "Crawler-all")]

        if to_write:
            self.write_lines(to_write)


class LogsHomePage(Container):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.logs_page = LogsPage(id="logs_page")

    def compose(self) -> ComposeResult:
        with ContentSwitcher(initial="option_list"):
            yield self.logs_page
            yield OptionList(
                Option("All crawlers", id="Crawler-all"),
                Option("MainThread", id="MainThread"),
                Option("QueueRecharger", id="QueueRecharger"),
                *(Option(f"Crawler-{i+1}", id=f"Crawler-{i+1}") for i in range(self.app.config.num_crawlers)),
                id="option_list"
            )

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.query_one(ContentSwitcher).current = "logs_page"
        self.logs_page.crawler_name = event.option_id
        self.logs_page.focus()


class DatabaseConsolePage(Container):
    BINDINGS = [
        Binding("f5", "execute_query", "Exécuter (F5)"),
        Binding("ctrl+j", "execute_query", "Exécuter (Ctrl+Enter)"),
    ]

    DEFAULT_CSS = """
    #password_container {
        height: auto;
    }
    #admin_pass_input {
        width: 1fr;
    }
    #toggle_password {
        min-width: 6;
        margin-left: 1;
    }
    """

    def __init__(
        self,
        *,
        id: str | None = None,
    ):
        super().__init__(id=id)

    def compose(self) -> ComposeResult:
        db_name = os.getenv("DB_NAME", "Inconnue")

        with ContentSwitcher(id="db_view_switcher"):
            # --- VUE 1 : FORMULAIRE DE CONNEXION ---
            with Container(id="login_view"):
                yield Label(
                    "🔒 Identifiants d'Administration PostgreSQL", id="login_title"
                )

                yield Input(
                    id="admin_user_input",
                    placeholder="Nom d'utilisateur admin (ex: postgres)",
                )

                with Horizontal(id="password_container"):
                    yield Input(
                        id="admin_pass_input", placeholder="Mot de passe", password=True
                    )
                    yield Button("👁️", id="toggle_password")

                yield Button(
                    "Sauvegarder et Accéder", id="save_admin_creds", variant="success"
                )

            # --- VUE 2 : CONSOLE POSTGRESQL ---
            with Container(id="console_view"):
                with Horizontal(id="console_header"):
                    yield Label(
                        f"Console PostgreSQL (Base restreinte : {db_name})",
                        id="console_title",
                    )
                    yield Button(
                        "Modifier les identifiants",
                        id="edit_admin_creds",
                        variant="warning",
                    )

                yield TextArea(
                    id="editor",
                    language="sql",
                )

                yield RichLog(
                    id="logs",
                    highlight=True,
                    markup=False,
                )

    def on_mount(self) -> None:
        admin_user = os.getenv("ADMIN_DB_USERNAME")
        admin_pass = os.getenv("ADMIN_DB_PASSWORD")

        switcher = self.query_one("#db_view_switcher", ContentSwitcher)

        if admin_user and admin_pass:
            switcher.current = "console_view"
        else:
            switcher.current = "login_view"

    @staticmethod
    def update_env_file(key: str, value: str) -> None:
        """Met à jour ou ajoute une clé/valeur dans le fichier .env de la racine du projet."""
        filepath = ".env"

        # On s'assure que le fichier existe, sinon on le crée
        if not os.path.exists(filepath):
            with open(filepath, "w", encoding="utf-8"):
                pass

        with open(filepath, "r", encoding="utf-8") as f:
            lines = f.readlines()

        updated = False
        for i, line in enumerate(lines):
            # On nettoie la ligne pour éviter les espaces parasites lors de la comparaison
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                lines[i] = f"{key}={value}\n"
                updated = True
                break

        # Si la variable n'était pas dans le .env, on l'écrit à la fin
        if not updated:
            # S'il manque un saut de ligne tout à la fin, on le rajoute pour ne pas coller les clés
            if lines and not lines[-1].endswith("\n"):
                lines.append("\n")
            lines.append(f"{key}={value}\n")

        with open(filepath, "w", encoding="utf-8") as f:
            f.writelines(lines)

    @staticmethod
    def interpret_postgres_error(output: str) -> str:
        """Analyse le message brut renvoyé par Postgres pour en faire une alerte claire."""
        if "password authentication failed" in output:
            return "Le mot de passe est incorrect."
        elif "role" in output and "does not exist" in output:
            return "Le nom d'utilisateur saisi n'existe pas."
        elif "database" in output and "does not exist" in output:
            return "La base de données spécifiée est introuvable."
        elif (
            "connection to server" in output
            or "could not connect to server" in output
            or "Connection refused" in output
        ):
            return "Connexion impossible : Vérifiez l'Hôte (DB_HOST), le Port (DB_PORT) ou si le serveur PostgreSQL est actif."

        return "Impossible de se connecter avec ces paramètres."

    async def on_button_pressed(self, event: Button.Pressed) -> None:

        # --- 1. AFFICHER / MASQUER LE MOT DE PASSE ---
        if event.button.id == "toggle_password":
            pass_input = self.query_one("#admin_pass_input", Input)
            pass_input.password = not pass_input.password
            event.button.label = "🙈" if pass_input.password else "👁️"

        # --- 2. SAUVEGARDER ET TESTER LA CONNEXION ---
        elif event.button.id == "save_admin_creds":
            user_val = self.query_one("#admin_user_input", Input).value.strip()
            pass_val = self.query_one("#admin_pass_input", Input).value

            if not user_val or not pass_val:
                self.app.notify(
                    "Veuillez remplir les deux champs.",
                    severity="error",
                    title="Erreur",
                )
                return

            db_host = os.getenv("DB_HOST", "localhost")
            db_port = os.getenv("DB_PORT", "5432")
            db_name = os.getenv("DB_NAME", "postgres")

            psql_env = os.environ.copy()
            psql_env["PGPASSWORD"] = pass_val

            event.button.label = "Test de connexion en cours..."
            event.button.disabled = True

            try:
                args = [
                    "psql",
                    "-h",
                    db_host,
                    "-p",
                    str(db_port),
                    "-U",
                    user_val,
                    "-d",
                    db_name,
                    "-w",
                    "-c",
                    "SELECT 1;",
                ]

                process = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    env=psql_env,
                )

                stdout_data, _ = await process.communicate()
                output = stdout_data.decode("utf-8", errors="replace")

                if process.returncode == 0:
                    # A. Sauvegarde dans la session courante de l'app
                    os.environ["ADMIN_DB_USERNAME"] = user_val
                    os.environ["ADMIN_DB_PASSWORD"] = pass_val

                    # B. Écriture / Mise à jour persistante dans le fichier .env
                    self.update_env_file("ADMIN_DB_USERNAME", user_val)
                    self.update_env_file("ADMIN_DB_PASSWORD", pass_val)

                    self.app.notify(
                        "Connexion réussie et sauvegardée !", severity="information"
                    )
                    self.query_one(
                        "#db_view_switcher", ContentSwitcher
                    ).current = "console_view"
                    self.query_one("#editor", TextArea).focus()
                else:
                    err_msg = self.interpret_postgres_error(output)
                    self.app.notify(
                        err_msg, severity="error", title="Échec de connexion"
                    )

            except FileNotFoundError:
                self.app.notify(
                    "L'outil 'psql' est introuvable sur le système.", severity="error"
                )
            except Exception as e:
                self.app.notify(f"Erreur inattendue : {e}", severity="error")
            finally:
                event.button.label = "Sauvegarder et Accéder"
                event.button.disabled = False

        # --- 3. MODIFIER LES IDENTIFIANTS ---
        elif event.button.id == "edit_admin_creds":
            self.query_one("#db_view_switcher", ContentSwitcher).current = "login_view"

            user_input = self.query_one("#admin_user_input", Input)
            user_input.value = os.getenv("ADMIN_DB_USERNAME", "")

            pass_input = self.query_one("#admin_pass_input", Input)
            pass_input.value = ""
            pass_input.password = True
            self.query_one("#toggle_password", Button).label = "👁️"

            user_input.focus()

    @property
    def editor(self) -> TextArea:
        return self.query_one("#editor", TextArea)

    @property
    def logs(self) -> RichLog:
        return self.query_one("#logs", RichLog)

    async def action_execute_query(self) -> None:
        if (
            self.query_one("#db_view_switcher", ContentSwitcher).current
            != "console_view"
        ):
            return

        query = self.editor.text.strip()

        if not query:
            return

        if re.search(r"^\\c(onnect)?\s+", query, re.IGNORECASE | re.MULTILINE):
            self.logs.write(f"\n=> {query}")
            self.logs.write(
                "❌ Action refusée : Le changement de base de données (\\c) n'est pas autorisé ici."
            )
            self.editor.text = ""
            return

        self.logs.write(f"\n=> {query}")
        self.editor.text = ""

        db_host = os.getenv("DB_HOST", "localhost")
        db_port = os.getenv("DB_PORT", "5432")
        db_name = os.getenv("DB_NAME", "postgres")

        admin_user = os.getenv("ADMIN_DB_USERNAME", "postgres")
        admin_pass = os.getenv("ADMIN_DB_PASSWORD", "")

        psql_env = os.environ.copy()
        psql_env["PGPASSWORD"] = admin_pass

        try:
            args = [
                "psql",
                "-h",
                db_host,
                "-p",
                str(db_port),
                "-U",
                admin_user,
                "-d",
                db_name,
                "-w",
            ]

            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=psql_env,
            )

            stdout_data, _ = await process.communicate(input=query.encode("utf-8"))

            output = stdout_data.decode("utf-8", errors="replace")
            self.logs.write(output)

        except FileNotFoundError:
            self.logs.write(
                "❌ Erreur : L'outil 'psql' est introuvable.\n"
                "Vérifiez que PostgreSQL est bien installé sur ce PC et que 'psql.exe' "
                "est dans les variables d'environnement (PATH) de Windows."
            )
        except Exception as e:
            self.logs.write(f"❌ Erreur inattendue : {e}")

# --- (Les autres pages comme DashboardPage, LogsPage, DatabaseConsolePage restent inchangées) ---


class ConfigInput(Input):
    def __init__(
        self,
        config_type: ConfigsTypes,
        config_path: list[str | int],
        value: str | None = None,
        placeholder: str = "",
        highlighter: Any = None,
        password: bool = False,
        restrict: str | None = None,
        id: str | None = None,
        classes: str | None = None,
        disabled: bool = False,
    ):
        # On passe explicitement les paramètres nommés pour éviter que l'IDE ne s'emmêle avec les arguments positionnels
        super().__init__(
            value=value,
            placeholder=placeholder,
            highlighter=highlighter,
            password=password,
            restrict=restrict,
            id=id,
            classes=classes,
            disabled=disabled,
        )
        self.config_type = config_type
        self.config_path = config_path


class ConfigsPage(Container):
    def __init__(self, config: CrawlerConfig, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.config = config

        self.type_restrictions = {
            int: r"^(?:-?\d+)?$",
            float: r"^(?:-?(?:\d+\.\d+|\d+\.?|\.\d+)(?:[eE][+-]?\d+)?)?$",
            str: r"^.*$",
            bool: r"^.*$",
            type(None): r"^.*$",
        }

    @staticmethod
    def _unwrap_union_type(tp: ConfigsTypesTree) -> ConfigsTypesTree:
        """Extrait le type sous-jacent d'une Union ou d'un Optional."""
        origin = get_origin(tp)
        if origin is Union or isinstance(tp, types.UnionType):
            non_none_types = [arg for arg in get_args(tp) if arg is not type(None)]
            if len(non_none_types) == 1:
                return cast(ConfigsTypesTree, non_none_types[0])
        return tp

    def get_restriction(self, tp: ConfigsTypes) -> str:
        """Détermine la regex de saisie pour un type donné (gère les Unions)."""
        origin = get_origin(tp)

        if origin is Union or isinstance(tp, types.UnionType):
            sub_restrictions = [self.get_restriction(arg) for arg in get_args(tp)]

            if r"^.*$" in sub_restrictions:
                return r"^.*$"

            clean_patterns = []
            for pat in sub_restrictions:
                if pat.startswith("^"):
                    pat = pat[1:]
                if pat.endswith("$"):
                    pat = pat[:-1]
                clean_patterns.append(pat)
            return f"^(?:{'|'.join(clean_patterns)})$"

        return self.type_restrictions.get(tp, r"^.*$")

    @staticmethod
    def _parse_scalar(
            value_str: str,
        target_type: type[int] | type[float] | type[str] | type[bool] | type[None],
    ) -> int | float | str | bool | None:
        """Convertit une chaîne brute vers son type scalaire cible de manière explicite."""
        value_stripped = value_str.strip()

        if target_type is type(None):
            if value_stripped == "None":
                return None
            raise ValueError("La valeur d'un champ nul doit être 'None'.")

        if target_type is bool:
            if value_stripped == "True":
                return True
            if value_stripped == "False":
                return False
            raise ValueError(
                "La valeur d'un champ booléen doit être 'True' ou 'False'."
            )

        try:
            if target_type is int:
                return int(value_stripped)
            if target_type is float:
                return float(value_stripped)
            if target_type is str:
                return str(value_stripped)
        except ValueError as err:
            name = getattr(target_type, "__name__", str(target_type))
            raise ValueError(
                f"Impossible de convertir '{value_str}' en {name}."
            ) from err

        raise ValueError(f"Type non supporté : {target_type}")

    def parse_value(
        self, value_str: str, config_type: ConfigsTypes
    ) -> int | float | str | bool | None:
        """Tente de parser la saisie utilisateur vers le type cible ou l'un des types de l'Union."""
        origin = get_origin(config_type)

        if origin is Union or isinstance(config_type, types.UnionType):
            for arg in get_args(config_type):
                if arg is type(None) and value_str.strip() == "None":
                    return None

            for arg in get_args(config_type):
                if arg is not type(None):
                    try:
                        return self.parse_value(value_str, cast(ConfigsTypes, arg))
                    except ValueError:
                        continue

            allowed_names = ", ".join(
                "None" if arg is type(None) else getattr(arg, "__name__", str(arg))
                for arg in get_args(config_type)
            )
            raise ValueError(
                f"La valeur '{value_str}' ne correspond à aucun type de l'union : [{allowed_names}]."
            )

        return self._parse_scalar(value_str, config_type)

    def compose(self) -> ComposeResult:
        for key, value, value_type, path in self.parse_config(
            self.config.model_dump(),
            self.get_type_tree(CrawlerConfig),
        ):
            with Horizontal():
                yield Label(key)

                if value_type not in [dict, list]:
                    # Cast explicite pour indiquer à l'analyseur que ce n'est plus un dict ou une liste
                    scalar_type = cast(ConfigsTypes, value_type)
                    yield ConfigInput(
                        config_type=scalar_type,
                        config_path=path,
                        value=str(value),
                        restrict=self.get_restriction(scalar_type),
                    )

    def on_input_submitted(self, event: Input.Submitted) -> None:
        config_input = cast(ConfigInput, event.input)

        if config_input.restrict and not re.match(config_input.restrict, event.value):
            self.notify("Format de saisie invalide.", severity="error")
            return

        try:
            value = self.parse_value(event.value, config_input.config_type)
        except ValueError as err:
            self.notify(str(err), severity="error", title="Erreur de validation")
            return

        self.set_config_value(config_input.config_path, value)
        self.config.save_to_yml(config_files_folder_path / "crawler_config.yaml")
        self.notify("Configuration enregistrée !", severity="information")

    def set_config_value(self, path: list[str | int], value: Any) -> None:
        obj = self.config

        for key in path[:-1]:
            if isinstance(key, int):
                # noinspection PyUnresolvedReferences
                obj = obj[key]
            else:
                obj = getattr(obj, key)

        last = path[-1]

        if isinstance(last, int):
            # noinspection PyUnresolvedReferences
            obj[last] = value
        else:
            setattr(obj, last, value)

    def get_type_tree(self, model: type[BaseModel]) -> ConfigsTypesTree:
        result = {}

        for name, field in model.model_fields.items():
            tp = field.annotation
            unwrapped_tp = self._unwrap_union_type(tp)

            if isinstance(unwrapped_tp, type) and issubclass(unwrapped_tp, BaseModel):
                result[name] = self.get_type_tree(unwrapped_tp)
            else:
                result[name] = tp

        return result

    def parse_config(
        self,
        config_dict: dict[str, Any] | list[Any] | None,
        config_type_dict: ConfigsTypesTree,
        path: list[str | int] | None = None,
    ) -> Generator[
        tuple[
            str,
            str,
            ConfigsTypes | type[dict[str, Any]] | type[list[Any]],
            list[str | int],
        ],
        None,
        None,
    ]:
        if path is None:
            path = []

        if config_dict is None:
            return

        if isinstance(config_dict, list):
            leaf_type: ConfigsTypes = type(None)
            unwrapped_type = self._unwrap_union_type(config_type_dict)
            args = get_args(unwrapped_type)
            if args:
                leaf_type = cast(ConfigsTypes, args[0])

            for index, value in enumerate(config_dict):
                yield "  - ", str(value), leaf_type, path + [index]
            return

        for key, value in config_dict.items():
            # Résolution propre pour éviter le retour implicite de "None" de dict.get()
            if isinstance(config_type_dict, dict):
                resolved_type = config_type_dict.get(key)
                value_type = resolved_type if resolved_type is not None else type(value)
            else:
                value_type = type(value)

            current_path = path + [key]
            unwrapped_value_type = self._unwrap_union_type(value_type) # type: ignore

            if isinstance(unwrapped_value_type, dict):
                if value is None:
                    yield f"{key}: ", "None", type(None), current_path
                else:
                    yield f"{key}:", "", dict, current_path
                    yield from (
                        ("  " + k, v, t, p)
                        for k, v, t, p in self.parse_config(
                            value, unwrapped_value_type, current_path
                        )
                    )

            elif (
                get_origin(unwrapped_value_type) is list or unwrapped_value_type is list
            ):
                if value is None:
                    yield f"{key}: ", "None", type(None), current_path
                else:
                    yield f"{key}:", "", list, current_path
                    yield from (
                        (k, v, t, p)
                        for k, v, t, p in self.parse_config(
                            value, unwrapped_value_type, current_path
                        )
                    )

            else:
                yield (
                    f"{key}: ",
                    str(value),
                    cast(ConfigsTypes, value_type),
                    current_path,
                )
                
                
class SecretsConfigsPage(Container):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        
        self.dotenv: Optional[str] = None
        if dotenv_file_path := find_dotenv():
            with open(dotenv_file_path, mode="r", encoding="utf-8") as f:
                self.dotenv = f.read()

    def on_mount(self) -> None:
        for input_widget in self.query(Input):
            input_widget.data = input_widget.value
        
    def compose(self) -> ComposeResult:
        if not self.dotenv:
            self.notify("File not found or empty", severity="warning")
            return

        for line in self.dotenv.strip().splitlines():
            if not line or line.startswith("#"):
                continue

            split_line = line.split("=", maxsplit=1)
            if len(split_line) != 2:
                continue

            with Horizontal():
                yield Label(split_line[0] + "=")
                yield Input(value=split_line[1], id=split_line[0])
    
    def on_input_submitted(self, event: Input.Submitted):
        if not self.dotenv:
            self.notify("File not found or empty", severity="warning")
            return

        if not event.value:
            self.notify("La valeur ne doit pas être vide !", severity="error")
            return

        # noinspection PyUnresolvedReferences
        self.dotenv = self.dotenv.replace(event.input.id + "=" + event.input.data, event.input.id + "=" + event.value)
        event.input.data = event.value
        with open(find_dotenv(), mode="w", encoding="utf-8") as f:
            f.write(cast(str, self.dotenv))

        self.notify("Secret config saved !", severity="information")


class CrawlerTerminalApp(App):
    def __init__(self, args: argparse.Namespace, cache: Cache | None = None):
        super().__init__()

        self.logs_home_page = LogsHomePage(id="logs_home")
        self.cache = cache

        self.config = CrawlerConfig.load_from_yml(config_files_folder_path / "crawler_config.yaml")

        self.stop_events = [threading.Event() for _ in range(self.config.num_crawlers + 1)]
        self.pause_events = [threading.Event() for _ in range(self.config.num_crawlers + 1)]
        self.crawlers, self.queue_recharger, self.observer = launch_crawler(args, self.stop_events, self.pause_events, cache, return_crawlers=True)

    @override
    def compose(self):
        yield Tabs(
            Tab("Dashboard", id="dashboard"),
            Tab("Logs", id="logs_home"),
            Tab("Database Console", id="database_console"),
            Tab("Configs", id="configs"),
            Tab("Secrets Configs", id="secrets_configs"),
        )

        with ContentSwitcher(initial="dashboard"):
            yield DashboardPage(self.crawlers, self.queue_recharger, self.cache, id="dashboard")
            yield self.logs_home_page
            yield DatabaseConsolePage(id="database_console")
            yield ConfigsPage(self.config, id="configs")
            yield SecretsConfigsPage(id="secrets_configs")

    def on_tabs_tab_activated(self, event: Tabs.TabActivated) -> None:
        """Appelé lorsqu'un onglet est activé (Entrée ou clic)."""
        switcher = self.query_one(ContentSwitcher)
        switcher.current = event.tab.id

        self.query_one("#" + event.tab.id).focus()

    @override
    async def action_quit(self) -> None:
        for stop_event in self.stop_events:
            stop_event.set()
        self.observer.stop()
            
        def join_threads():
            for crawler in self.crawlers:
                list(crawler.keys())[0].join()
            list(self.queue_recharger.keys())[0].join()
            self.observer.join()
        
        await asyncio.to_thread(join_threads)
        
        await super().action_quit()


class TextualSink:
    def __init__(
        self,
        app: CrawlerTerminalApp,
        default_ui_level: LoggingLevels = LoggingLevels.INFO,
    ):
        self.app = app

        level_map = {
            LoggingLevels.TRACE: 0,
            LoggingLevels.DEBUG: 1,
            LoggingLevels.INFO: 2,
            LoggingLevels.SUCCESS: 3,
            LoggingLevels.WARNING: 4,
            LoggingLevels.ERROR: 5,
            LoggingLevels.CRITICAL: 6,
            LoggingLevels.FATAL: 7,
        }
        self.app.logs_home_page.logs_page.current_level_index = level_map.get(default_ui_level, 2)

    def write(self, message: Message):
        if not getattr(self.app, "is_running", True):
            return

        level_no = message.record["level"].no
        crawler_name = message.record["thread"].name

        # On nettoie le saut de ligne à la fin pour que write_lines() gère le scroll proprement
        raw_msg = str(message).rstrip("\n")

        page = self.app.logs_home_page.logs_page

        with page.buffer_lock:
            page.log_buffer.append((level_no, crawler_name, raw_msg))

            if len(page.log_buffer) > 2000:
                del page.log_buffer[:1000]
