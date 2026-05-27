"""
Code Graph Builder — Neo4j Integration for Planning Layer
---------------------------------------------------------
Строит граф зависимостей Python кода:
- Модули и их импорты
- Функции и классы
- Вызовы функций
- Наследование классов

Используется ТОЛЬКО в Planning слое для точного определения context_files.
"""

import ast
import os
from pathlib import Path
from typing import Optional

from neo4j import GraphDatabase
from rich.console import Console

console = Console()


class CodeGraph:
    """Управление графом зависимостей кода в Neo4j."""

    def __init__(
        self,
        uri: str = "bolt://localhost:7687",
        user: str = "neo4j",
        password: str = "developer-farm-2026",
        database: str = "neo4j",
    ):
        self.driver = GraphDatabase.driver(uri, auth=(user, password))
        self.database = database
        console.print(f"[green]✅ Connected to Neo4j: {uri}[/]")

    def close(self):
        self.driver.close()

    def clear_graph(self):
        """Очищает граф перед перестроением."""
        with self.driver.session(database=self.database) as session:
            session.run("MATCH (n) DETACH DELETE n")
        console.print("[yellow]🧹 Graph cleared[/]")

    def index_codebase(self, root_path: str, file_patterns: list[str] = None):
        """
        Индексирует всю кодовую базу в Neo4j.

        Args:
            root_path: Корневая директория проекта
            file_patterns: Паттерны файлов для индексации (по умолчанию ["*.py"])
        """
        if file_patterns is None:
            file_patterns = [
                "nodes/*.py",
                "graph/*.py",
                "utils/*.py",
                "dashboard/*.py",
                "contracts.py",
                "run_pipeline.py",
            ]

        self.clear_graph()

        root = Path(root_path)
        py_files = []
        for pattern in file_patterns:
            py_files.extend(root.glob(pattern))

        console.print(f"[cyan]📂 Indexing {len(py_files)} files...[/]")

        # Создаём узлы для всех файлов
        with self.driver.session(database=self.database) as session:
            for py_file in py_files:
                rel_path = str(py_file.relative_to(root))
                module_name = rel_path.replace("/", ".").replace(".py", "")

                # Создаём узел Module
                session.run(
                    """
                    MERGE (m:Module {path: $path, name: $name})
                    SET m.last_indexed = timestamp()
                    """,
                    path=rel_path,
                    name=module_name,
                )

        # Парсим каждый файл и создаём связи
        for py_file in py_files:
            self._parse_file(py_file, root)

        console.print(f"[green]✅ Indexed {len(py_files)} modules[/]")

    def _parse_file(self, file_path: Path, root: Path):
        """Парсит Python файл и создаёт узлы/связи в графе."""
        try:
            code = file_path.read_text(encoding="utf-8")
            tree = ast.parse(code, filename=str(file_path))
        except Exception as e:
            console.print(f"[yellow]⚠ Cannot parse {file_path}: {e}[/]")
            return

        rel_path = str(file_path.relative_to(root))
        module_name = rel_path.replace("/", ".").replace(".py", "")

        with self.driver.session(database=self.database) as session:
            # Создаём узлы для функций и классов
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    session.run(
                        """
                        MATCH (m:Module {path: $path})
                        MERGE (f:Function {name: $name, module: $module})
                        MERGE (m)-[:DEFINES]->(f)
                        SET f.lineno = $lineno, f.args = $args
                        """,
                        path=rel_path,
                        name=node.name,
                        module=module_name,
                        lineno=node.lineno,
                        args=[arg.arg for arg in node.args.args],
                    )

                elif isinstance(node, ast.ClassDef):
                    session.run(
                        """
                        MATCH (m:Module {path: $path})
                        MERGE (c:Class {name: $name, module: $module})
                        MERGE (m)-[:DEFINES]->(c)
                        SET c.lineno = $lineno
                        """,
                        path=rel_path,
                        name=node.name,
                        module=module_name,
                        lineno=node.lineno,
                    )

                    # Наследование
                    for base in node.bases:
                        if isinstance(base, ast.Name):
                            session.run(
                                """
                                MATCH (c:Class {name: $class_name, module: $module})
                                MERGE (base:Class {name: $base_name})
                                MERGE (c)-[:INHERITS]->(base)
                                """,
                                class_name=node.name,
                                module=module_name,
                                base_name=base.id,
                            )

            # Импорты
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        session.run(
                            """
                            MATCH (m:Module {path: $path})
                            MERGE (imported:Module {name: $imported_name})
                            MERGE (m)-[:IMPORTS]->(imported)
                            """,
                            path=rel_path,
                            imported_name=alias.name,
                        )

                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        session.run(
                            """
                            MATCH (m:Module {path: $path})
                            MERGE (imported:Module {name: $imported_name})
                            MERGE (m)-[:IMPORTS]->(imported)
                            """,
                            path=rel_path,
                            imported_name=node.module,
                        )

    def find_related_files(self, target_path: str, depth: int = 2) -> list[str]:
        """
        Находит файлы, связанные с target_path через импорты и зависимости.

        Args:
            target_path: Путь к целевому файлу (например "src/auth/jwt.py")
            depth: Глубина обхода графа (1 = прямые импорты, 2 = импорты импортов)

        Returns:
            Список путей к связанным файлам
        """
        with self.driver.session(database=self.database) as session:
            result = session.run(
                """
                MATCH (m:Module {path: $path})
                OPTIONAL MATCH (m)-[:IMPORTS*1..$depth]-(related:Module)
                WITH m, collect(DISTINCT related.path) as imports
                OPTIONAL MATCH (m)<-[:DEFINES]-(func:Function)<-[:CALLS]-(caller:Function)<-[:DEFINES]-(caller_module:Module)
                WITH m, imports, collect(DISTINCT caller_module.path) as callers
                RETURN imports + callers as related_files
                """,
                path=target_path,
                depth=depth,
            )

            record = result.single()
            if record:
                related = [f for f in record["related_files"] if f and f != target_path]
                return list(set(related))  # Убираем дубликаты
            return []

    def get_module_dependencies(self, module_name: str) -> dict:
        """
        Возвращает полную информацию о зависимостях модуля.

        Returns:
            {
                "imports": ["module1", "module2"],
                "imported_by": ["module3", "module4"],
                "functions": ["func1", "func2"],
                "classes": ["Class1", "Class2"]
            }
        """
        with self.driver.session(database=self.database) as session:
            # Импорты
            imports_result = session.run(
                """
                MATCH (m:Module {name: $name})-[:IMPORTS]->(imported:Module)
                RETURN imported.name as import
                """,
                name=module_name,
            )
            imports = [record["import"] for record in imports_result]

            # Кто импортирует этот модуль
            imported_by_result = session.run(
                """
                MATCH (importer:Module)-[:IMPORTS]->(m:Module {name: $name})
                RETURN importer.name as importer
                """,
                name=module_name,
            )
            imported_by = [record["importer"] for record in imported_by_result]

            # Функции
            functions_result = session.run(
                """
                MATCH (m:Module {name: $name})-[:DEFINES]->(f:Function)
                RETURN f.name as function
                """,
                name=module_name,
            )
            functions = [record["function"] for record in functions_result]

            # Классы
            classes_result = session.run(
                """
                MATCH (m:Module {name: $name})-[:DEFINES]->(c:Class)
                RETURN c.name as class
                """,
                name=module_name,
            )
            classes = [record["class"] for record in classes_result]

            return {
                "imports": imports,
                "imported_by": imported_by,
                "functions": functions,
                "classes": classes,
            }

    def query(self, cypher: str, **params):
        """Выполняет произвольный Cypher-запрос."""
        with self.driver.session(database=self.database) as session:
            result = session.run(cypher, **params)
            return [record.data() for record in result]
