# Contributing to Project Athena

Thank you for your interest in contributing to Project Athena! This document provides guidelines for contributing to the project.

## Code of Conduct

By participating in this project, you agree to maintain a respectful and inclusive environment for everyone.

## How to Contribute

### Reporting Issues

1. Check if the issue already exists in the [Issues](https://github.com/jstuart0/project-athena-oss/issues) tab
2. If not, create a new issue with:
   - A clear, descriptive title
   - Steps to reproduce (if applicable)
   - Expected vs actual behavior
   - Environment details (OS, Python version, etc.)

### Submitting Pull Requests

1. **Fork the repository**
   ```bash
   # Clone your fork
   git clone https://github.com/YOUR_USERNAME/project-athena-oss.git
   cd project-athena-oss

   # Add upstream remote
   git remote add upstream https://github.com/jstuart0/project-athena-oss.git
   ```

2. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

3. **Make your changes**
   - Follow the existing code style
   - Add tests if applicable
   - Update documentation as needed

4. **Commit your changes**
   ```bash
   git commit -m "Add brief description of changes"
   ```

5. **Keep your branch up to date**
   ```bash
   git fetch upstream
   git rebase upstream/main
   ```

6. **Push and create a PR**
   ```bash
   git push origin feature/your-feature-name
   ```
   Then open a Pull Request on GitHub.

### Pull Request Guidelines

- Provide a clear description of the changes
- Reference any related issues
- Ensure all tests pass
- Keep changes focused and atomic
- Be responsive to feedback

## Development Setup

1. **Clone and setup**
   ```bash
   git clone https://github.com/jstuart0/project-athena-oss.git
   cd project-athena-oss
   python -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Configure environment**
   ```bash
   cp .env.example .env
   # Edit .env with your configuration
   ```

3. **Run tests**

   The test suite uses `pytest.ini` to register an `integration` marker. By default, running `pytest` executes only unit tests (tests not marked `integration`):

   ```bash
   # Unit tests only (default — no live services required)
   pytest tests/

   # Include integration tests (require live PostgreSQL, Redis, and running services)
   pytest -m integration tests/
   ```

   Mark tests that require live services with `@pytest.mark.integration`. New tests should be unit tests unless they genuinely require external state.

## Code Style

- Use Python 3.11+ features
- Follow PEP 8 guidelines
- Use type hints where practical
- Keep functions focused and well-documented

## Configuration Guidelines

When contributing code that requires configuration:

- **Never hardcode** IP addresses, hostnames, passwords, or API keys
- Read configuration via `os.getenv("VAR_NAME")` (or `src/shared/config.py` for values already centralized there); prefer `os.getenv` directly for new code — not all services import `config.py`
- Add every new environment variable to `.env.example` with a clear inline comment describing its purpose and an example value; this is the canonical reference for deployers
- Use sensible defaults that work for local development, and emit a log warning when a required variable is missing rather than failing silently or falling back to a hardcoded value

## Module Development

When adding new modules or RAG services:

1. Register the module in `shared/module_registry.py`
2. Add appropriate environment variable controls
3. Ensure the module gracefully handles being disabled
4. Document the module in `docs/MODULES.md`

## Questions?

If you have questions about contributing, please open an issue with the "question" label.

## License

By contributing to Project Athena, you agree that your contributions will be licensed under the PolyForm Noncommercial License 1.0.0.
