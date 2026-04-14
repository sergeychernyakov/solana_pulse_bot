# PYTHON\_STYLE\_GUIDE.md

## 🐍 **Python Development Prompt**

## 📦 General Guidelines

* Write **clean**, **readable**, and **modular** code.
* Follow **DRY**, **KISS**, and **YAGNI** principles.
* Use meaningful, descriptive names for all entities.
* The **first line of each file** must be a comment with the file path, e.g.:

  ```python
  # src/services/user_service.py
  ```
* Use **list comprehensions** and **generator expressions** where appropriate.
* One file = one responsibility. No dumping ground modules.
* Consistent naming across the repo. Be a naming ninja.
* Don't use argparse unless explicitly instructed

---

## 🧱 Naming Conventions (Rails-style for Python)

### ✅ File Names (snake\_case)

* Lowercase with underscores.
* Example: `user_controller.py`, `order_mailer.py`, `payment_service.py`

### ✅ Class Names (CamelCase)

* Use singular nouns.
* Example: `UserController`, `InvoiceMailer`, `PaymentService`

### ✅ Method & Function Names (snake\_case)

* Use action-based, meaningful verbs.
* Example: `send_email`, `create_user`, `process_payment`

### ✅ Test File Naming

* Match source file: `user_service.py` → `test_user_service.py`
* Method format: `test_create_user_with_valid_data()`

---

## ⚙️ OOP & Design Patterns

* Use OOP principles: **encapsulation**, **abstraction**, **polymorphism**, **inheritance**.
* Favor **composition over inheritance**.
* Follow **SOLID** principles.
* Wrap logic into service objects: `CsvToExcelExporter`, not random functions.
* Use design patterns:

  * `Singleton`, `Factory`, `Strategy`, `Observer`, `Dependency Injection`
* Decouple logic with **interfaces** (`abc.ABC`).

---

## 📝 Type Hints & Docstrings

* Type-annotate all public functions, methods, and attributes.
* Prefer specific types (`str`, `int`, `Dict[str, Any]`) over `Any`.
* Avoid `Optional[None]` unless needed.
* Use Google-style docstrings:

  ```python
  def send_email(to: str, subject: str, body: str) -> None:
      """Sends an email using the default SMTP backend.

      Args:
          to (str): Recipient's email address.
          subject (str): Email subject.
          body (str): Email body text.

      Returns:
          None
      """
  ```
* Use `@overload` when function signatures vary.

---

## 🧪 Testing (pytest)

* Use `pytest` for all tests.
* Follow AAA pattern: **Arrange → Act → Assert**.
* Target **90%+ test coverage**.
* tests should mirror the application structure
* Cover:

  * Happy paths ✅
  * Edge cases ⚠️
  * Exceptions 💣
* Use:

  * `pytest-mock`, `unittest.mock`
  * Fixtures for reusable setups
  * `@pytest.mark.parametrize` for clean data-driven tests

---

## ✅ Linting & Code Quality

* Follow **PEP 8**.
* Tools:

  * `black` (formatting)
  * `isort` (import sorting)
  * `flake8` (style guide)
  * `pylint` (target score ≥ 9.5)
* Avoid:

  * Long functions → break them up
  * Magic numbers → name them
  * Cyclic imports → restructure
  * `from module import *` → ban it
* Max line length: 100 chars

---

## 🪵 Logging & Error Handling

* Use `logging`, never `print()`.
* Log levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`
* Don’t log secrets or PII.
* Use lazy formatting:

  ```python
  logger.info("Processed invoice %s for user %s", invoice_id, user_id)
  ```
* Raise **custom exceptions** with helpful messages.
* Avoid bare `except:` — catch specific exceptions.
* Prefer structured logging (e.g., JSON) in production.

---

## 🚀 Performance Tips

* Choose correct data structures: `dict`, `set`, `deque`, etc.
* Use `functools.lru_cache` for caching.
* Profile with `cProfile` before optimizing.
* For IO-bound: `asyncio`, `aiohttp`
* For CPU-bound: `multiprocessing`, not `asyncio`

---

## 🔐 Security Best Practices

* Never hardcode secrets — use `.env` or secret managers.
* Sanitize user inputs.
* Escape all outputs in web apps.
* Use parameterized queries with SQLAlchemy/psycopg2.
* Audit dependencies with `pip-audit`, `safety`, or `poetry check`.

---

## 📦 Deliverables

* Clean, modular code with Rails-style naming.
* 100% type-annotated code.
* Google-style docstrings for all public APIs.
* 90%+ test coverage via `pytest`.
* Fully linted with `black`, `isort`, `flake8`, `pylint (≥9.5)`.
* Structured logging and clean error handling.
* English-only code comments and docs.
* Code structured around services, not scripts.
