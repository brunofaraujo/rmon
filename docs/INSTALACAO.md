# Instalação (produção)

Guia de instalação do RMonitor numa VM Linux dedicada. A aplicação roda como
serviço `systemd` e monitora servidores Windows remotos — **nada é instalado nos
alvos**; eles só precisam ter o WinRM habilitado (ver [CONFIGURACAO.md](CONFIGURACAO.md#winrm-nos-servidores-windows)).

## 1. Pré-requisitos

| Item | Requisito |
|---|---|
| SO da VM | Ubuntu 22.04+ / Debian 12+ (com `systemd` e `apt`) |
| Acesso | usuário com `sudo` na VM |
| Rede | alcance aos alvos em **WinRM** (5985 ou 5986) e, se usar jobs, ao **SQL Server** (1433) |
| Banco | PostgreSQL — **provisionado automaticamente** pelo instalador (local) |

O instalador cuida do Python, do virtualenv, do PostgreSQL e das bibliotecas do
`pymssql` (FreeTDS). Você não precisa instalá-los à mão.

## 2. Obter o código na VM

Via `git`:

```bash
git clone https://github.com/<seu-usuario>/rmon.git
cd rmon
```

Ou copie da sua estação com `scp`/`rsync` para um diretório qualquer (ex.: `~/rmon-src`).

## 3. Rodar o instalador

```bash
sudo deploy/instalar.sh
```

Ele é **idempotente** (pode rodar de novo sem estragar nada) e executa, em ordem:

1. **Pacotes de SO** — `python3-venv`, `postgresql`, `freetds-dev`, `build-essential`, etc.
2. **Usuário de serviço** `rmon` (system user, sem shell).
3. **Cópia** da aplicação para `/opt/rmon`.
4. **Virtualenv** em `/opt/rmon/.venv` + `pip install -r requirements.txt`.
5. **PostgreSQL** — cria a role `rmon` e o database `rmon`, gera uma senha aleatória e monta o `RMON_DB_DSN`.
6. **Inventário** — cria `config/servers.yaml` a partir do exemplo (se ainda não existir).
7. **Segredos** — gera `/opt/rmon/.env` (perm `600`), perguntando **interativamente** a senha do admin do painel e (opcional) a credencial WinRM.
8. **Serviço systemd** `rmon` — habilita, sobe e valida `http://127.0.0.1:8080/healthz`.

### Modo não-interativo

Para automação (sem perguntar senhas), exporte `RMON_NONINTERACTIVE=1`. O `.env` é
criado **sem** a senha do admin — defina-a depois com o helper (passo 5 abaixo).

```bash
sudo RMON_NONINTERACTIVE=1 deploy/instalar.sh
```

## 4. Ajustar o inventário

Edite os servidores monitorados:

```bash
sudoedit /opt/rmon/config/servers.yaml
sudo systemctl restart rmon
```

Estrutura e campos: veja [CONFIGURACAO.md](CONFIGURACAO.md#inventário-configserversyaml).

## 5. Definir segredos (helpers)

Todos gravam **apenas** em `/opt/rmon/.env` (perm `600`) e reiniciam o serviço.
Senhas são digitadas sem eco e não vão para o histórico do shell.

```bash
sudo deploy/definir-senha-admin.sh          # senha do admin do painel (hash pbkdf2)
sudo deploy/definir-winrm.sh fin            # credencial WinRM do perfil 'fin'
sudo deploy/definir-winrm.sh rh             # ... e de outros perfis
sudo deploy/definir-sql.sh                  # login SQL (somente-leitura) p/ estatísticas de jobs
sudo deploy/definir-telegram.sh             # alerta via Telegram (+ mensagem de teste)
sudo deploy/definir-slack.sh                # alerta via Slack (+ mensagem de teste)
```

## 6. Primeiro acesso

Abra `http://<ip-da-vm>:8080` e faça login com o usuário admin definido no passo 3/5.
O admin inicial é semeado no banco a partir do `.env` no primeiro start.

Para criar mais usuários, use a CLI (ver [OPERACAO.md](OPERACAO.md#usuários-do-painel)).

## 7. Firewall (recomendado)

Restrinja a porta do painel à sua rede de gestão. Exemplo com `ufw`:

```bash
sudo ufw allow from 10.0.0.0/24 to any port 8080 proto tcp
sudo ufw allow from 10.0.0.0/24 to any port 22 proto tcp
sudo ufw default deny incoming && sudo ufw enable
```

A 8080 tem **dois públicos**, e o comentário da regra é o que os distingue:

- **quem abre o painel** — sua rede de gestão e as estações/TVs do mural;
- **quem baixa pacote** — cada host Windows monitorado busca o pacote de execução na 8080
  (`execution.base_url`), então precisa de uma liberação própria, host a host.

> **O comentário da regra de entrega precisa conter a palavra `pacote`.** É por esse
> substring que o `definir-https.sh` (passo 7.1) reconhece o host de entrega e **não** lhe abre
> 80/443. Uma regra de entrega comentada de outro jeito — ou sem comentário — é tratada como
> cliente do painel e ganha acesso HTTPS, sem aviso nenhum.

```bash
# quem abre o painel
sudo ufw allow from 10.0.0.9 to any port 8080 proto tcp comment "RMon painel - 10.0.0.9"
# quem baixa pacote
sudo ufw allow from 10.0.0.34 to any port 8080 proto tcp comment "RMon - entrega de pacote 10.0.0.34"
```

Depois do passo 7.1 o conjunto fica assim (`sudo ufw status`) — as linhas `80,443/tcp` são
criadas pelo `definir-https.sh`, uma por origem de painel; a entrega de pacote continua só na
8080 em HTTP, por causa do comentário:

```
To                 Action      From
--                 ------      ----
22/tcp             ALLOW IN    10.0.0.0/24
8080/tcp           ALLOW IN    10.0.0.0/24
8080/tcp           ALLOW IN    10.0.0.9      # RMon painel - 10.0.0.9
8080/tcp           ALLOW IN    10.0.0.34     # RMon - entrega de pacote 10.0.0.34
80,443/tcp         ALLOW IN    10.0.0.0/24   # RMon HTTPS - 10.0.0.0/24
80,443/tcp         ALLOW IN    10.0.0.9      # RMon HTTPS - 10.0.0.9
```

Ao desativar um host do inventário, remova a regra de entrega dele
(`sudo ufw status numbered` e `sudo ufw delete <n>`) — o RMon não mexe no firewall por conta.

## 7.1 HTTPS (nginx + certificado próprio)

Navegadores com política *somente HTTPS* não abrem `http://...:8080`. O helper abaixo põe
um **nginx** na 443 terminando o TLS e repassando ao uvicorn — a 8080 continua em HTTP,
porque é por ela que os hosts Windows baixam os pacotes (`execution.base_url`):

```bash
sudo bash deploy/definir-https.sh                # libera 80/443 para as mesmas origens da 8080
sudo bash deploy/definir-https.sh 10.0.0.0/24    # ou informe as origens explicitamente
```

O que ele faz (idempotente):

- cria uma **CA local** (`/etc/rmon/tls/ca/`, 10 anos) e assina com ela o certificado do
  servidor (825 dias) com SAN para o hostname e os IPs da VM (extras em `RMON_TLS_SAN`,
  ex.: `RMON_TLS_SAN="DNS:rmon.empresa.local"`);
- instala o site `deploy/nginx-rmon.conf` — 443 com TLS, 80 redireciona para a 443;
- publica a CA em `http://<host>/rmon-ca.crt` para instalar nos clientes;
- liga o timer `rmon-tls-renovar.timer`, que reemite o certificado do servidor 60 dias
  antes de vencer **com a mesma CA** (quem já confia nela não percebe a troca);
- **libera 80/443 no ufw**: sem argumentos, lê as origens já liberadas na 8080 — **menos as
  regras cujo comentário contém `pacote`** (ver passo 7) e as IPv6 — e replica cada uma como
  `80,443/tcp ALLOW` com comentário `RMon HTTPS - <origem>`; com argumentos, usa só as
  origens informadas, sem consultar a 8080. Só emite `ufw allow` — nunca apaga nem altera
  regra existente, e não toca na política default. Com ufw inativo, apenas avisa e segue.

> A 80 é liberada junto da 443 porque é por ela que a CA é publicada
> (`http://<host>/rmon-ca.crt`); fora disso ela só redireciona para a 443.

Sem instalar a CA o navegador mostra o aviso de certificado. Para sumir com ele, instale
`rmon-ca.crt` como **raiz confiável** nas máquinas que abrem o painel:

- **Windows (uma máquina)**: `certutil -addstore -f Root rmon-ca.crt` (prompt como admin).
- **Windows (domínio)**: GPO → *Configuração do Computador → Políticas → Configurações do
  Windows → Configurações de Segurança → Políticas de Chave Pública → Autoridades de
  Certificação Raiz Confiáveis* → Importar.
- **TV / Android**: *Configurações → Segurança → Criptografia e credenciais → Instalar
  certificado → Certificado de CA*.

Confira a impressão digital SHA-256 que o script imprime antes de confiar na CA. Se a
empresa tiver uma CA interna (AD CS), prefira um certificado emitido por ela: sobrescreva
`/etc/rmon/tls/rmon.crt`/`rmon.key`, rode `sudo systemctl reload nginx` e desligue o timer,
que reemitiria com a CA local: `sudo systemctl disable --now rmon-tls-renovar.timer`.

O nginx **não** envia HSTS de propósito: com CA própria, o HSTS impediria contornar o aviso
em quem ainda não instalou a CA.

## 8. Verificar

```bash
systemctl status rmon
curl -fsS http://127.0.0.1:8080/healthz    # {"status":"ok","version":"..."}
journalctl -u rmon -f                       # logs ao vivo
```

## Desinstalação

```bash
sudo systemctl disable --now rmon
sudo rm -f /etc/systemd/system/rmon.service
sudo systemctl daemon-reload
sudo deluser --remove-home rmon 2>/dev/null || true
sudo rm -rf /opt/rmon
# Banco (opcional — apaga o histórico):
sudo runuser -u postgres -- dropdb rmon
sudo runuser -u postgres -- dropuser rmon
```
