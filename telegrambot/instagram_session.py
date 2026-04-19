import argparse
from pathlib import Path

import instaloader

BASE_DIR = Path(__file__).parent
SESSION_PATH = BASE_DIR / "instagram.session"
SESSION_USER_PATH = BASE_DIR / "instagram_session_user.txt"


def save_session(username: str, password: str | None):
    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        quiet=False,
        max_connection_attempts=1,
    )
    if password:
        loader.login(username, password)
    else:
        loader.interactive_login(username)

    logged_user = loader.test_login()
    if not logged_user:
        raise SystemExit("Instagram no devolvió una sesión válida.")

    loader.save_session_to_file(str(SESSION_PATH))
    SESSION_USER_PATH.write_text(f"{logged_user}\n")
    print(f"Sesion guardada para {logged_user} en {SESSION_PATH}")


def check_session():
    if not SESSION_PATH.exists() or not SESSION_USER_PATH.exists():
        raise SystemExit("No hay sesión guardada.")

    username = SESSION_USER_PATH.read_text().strip()
    if not username:
        raise SystemExit("El archivo de usuario de sesión está vacío.")

    loader = instaloader.Instaloader(
        download_pictures=False,
        download_videos=False,
        download_video_thumbnails=False,
        download_geotags=False,
        download_comments=False,
        save_metadata=False,
        quiet=False,
        max_connection_attempts=1,
    )
    loader.load_session_from_file(username, str(SESSION_PATH))
    logged_user = loader.test_login()
    if not logged_user:
        raise SystemExit("La sesión no es válida.")
    print(f"Sesion válida para {logged_user}")


def main():
    parser = argparse.ArgumentParser(
        description="Crea o valida la sesión de Instagram usada por la VM."
    )
    parser.add_argument("--username", help="Usuario de Instagram para guardar la sesión")
    parser.add_argument(
        "--password",
        help="Password de Instagram. Si falta, Instaloader pide login interactivo.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Valida la sesión guardada sin pedir credenciales",
    )
    args = parser.parse_args()

    if args.check:
        check_session()
        return

    if not args.username:
        raise SystemExit("Falta --username")

    save_session(args.username, args.password)


if __name__ == "__main__":
    main()
