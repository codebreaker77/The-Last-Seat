"""Generate local credentials without checking secrets into Git."""
from pathlib import Path
import os
import secrets

path = Path(__file__).resolve().parents[1] / '.env'
if path.exists():
    print('.env already exists; preserving credentials.')
else:
    lines = [f'{name}={secrets.token_hex(24)}' for name in
             ['POSTGRES_PASSWORD', 'RABBITMQ_PASSWORD', 'ADMIN_KEY']]
    if hasattr(os, 'getuid'):
        lines.extend([f'LOCAL_UID={os.getuid()}', f'LOCAL_GID={os.getgid()}'])
    path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('Created .env. Start with: docker compose up --build -d')
