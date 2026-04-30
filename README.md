# LinkedIn RSS Scraper

Scraper de posts de páginas de empresa de LinkedIn que genera automáticamente un archivo `feed.xml` en formato RSS estándar. El feed se actualiza cada 6 horas mediante GitHub Actions (plan gratuito).

## Cómo funciona

1. Selenium controla Chrome en modo headless para autenticarse en LinkedIn.
2. El script navega a la sección `/posts/` de cada empresa configurada y extrae los posts.
3. Se genera `feed.xml` con los posts en formato RSS 2.0.
4. GitHub Actions ejecuta todo automáticamente y hace commit del `feed.xml` actualizado.
5. La sesión autenticada se persiste entre ejecuciones usando la caché de Actions, evitando inicios de sesión frecuentes.

## Requisitos

- Python 3.11+
- Google Chrome instalado
- Cuenta de LinkedIn

## Configuración inicial (local)

### 1. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 2. Crear el archivo `.env`

```bash
cp .env.example .env
```

Edita `.env` y rellena tus credenciales de LinkedIn:

```
LINKEDIN_EMAIL=tu@email.com
LINKEDIN_PASSWORD=tu_contraseña
```

### 3. Primer login (genera `session.json`)

Ejecuta el scraper una vez en local. Si tu cuenta usa verificación en dos pasos o LinkedIn muestra un CAPTCHA, esto lo resuelve en la primera ejecución:

```bash
python scraper.py
```

El script generará `session.json` con las cookies de sesión. **Este archivo está en `.gitignore`** y nunca debe subirse al repositorio.

> **Nota sobre CAPTCHA / 2FA:** Si LinkedIn bloquea el login headless, edita temporalmente `build_driver()` en `scraper.py` y elimina `--headless=new` de las opciones de Chrome. Ejecuta el script, completa la verificación manualmente en el navegador y el `session.json` se guardará. Luego vuelve a activar el modo headless.

## Configuración en GitHub Actions

### 1. Añadir secretos al repositorio

Ve a **Settings → Secrets and variables → Actions → New repository secret** y añade:

| Nombre | Valor |
|---|---|
| `LINKEDIN_EMAIL` | Tu email de LinkedIn |
| `LINKEDIN_PASSWORD` | Tu contraseña de LinkedIn |

### 2. Activar el workflow

El workflow `.github/workflows/update-feed.yml` se ejecuta automáticamente cada 6 horas. También puedes lanzarlo manualmente desde **Actions → Update LinkedIn RSS Feed → Run workflow**.

### 3. Persistencia de sesión

GitHub Actions guarda `session.json` en su caché entre ejecuciones, de modo que el login completo con usuario/contraseña solo ocurre cuando la sesión expira o se limpia la caché.

## Añadir más páginas de empresa

Edita la lista `COMPANY_URLS` al inicio de `scraper.py`:

```python
COMPANY_URLS = [
    "https://www.linkedin.com/company/eoa-enhancing-opportunities-for-all/",
    "https://www.linkedin.com/company/otra-empresa/",   # ← añade aquí
    "https://www.linkedin.com/company/una-mas/",
]
```

No hay ningún otro cambio necesario.

## Consumir el feed RSS

Una vez que el workflow haya ejecutado al menos una vez, el `feed.xml` estará disponible en:

```
https://raw.githubusercontent.com/pablopernia11-bit/linkedin-rss-scraper/main/feed.xml
```

Puedes suscribirte a esta URL con cualquier lector RSS (Feedly, NetNewsWire, etc.).

## Solución de problemas

| Problema | Causa probable | Solución |
|---|---|---|
| `No posts were scraped` | LinkedIn cambió sus clases CSS | Actualiza los selectores en `scraper.py` (sección `TEXT_SELECTORS`, `LINK_SELECTORS`, etc.) |
| `Login timed out` | CAPTCHA o 2FA | Ejecuta en local sin `--headless`, completa la verificación, sube el `session.json` codificado como secreto |
| El workflow falla con `403` | Token de GitHub sin permisos de escritura | Activa *Read and write permissions* en **Settings → Actions → General → Workflow permissions** |

## Aviso legal

Este proyecto es para uso personal y educativo. El scraping de LinkedIn puede estar en contra de sus [Términos de Servicio](https://www.linkedin.com/legal/user-agreement). Úsalo bajo tu propia responsabilidad.
