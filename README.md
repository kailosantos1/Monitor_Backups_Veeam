# Monitor de Backups — Veeam Backup & Replication

Painel interno que mostra, máquina por máquina, se o Veeam está **de fato** fazendo backup.
Lê a REST API do VBR (somente leitura). Não altera nada no Veeam.

## O princípio

Cada máquina tem duas evidências independentes, ambas vindas do Veeam:

1. **Último ponto de restauração**: a data do ponto mais novo que existe hoje no Veeam.
   É a prova de que existe backup. Se for mais velho que o RPO, ou não existir, a máquina fica
   **SEM BACKUP** (vermelho), independentemente de qualquer outra coisa.
2. **Último resultado**: o resultado da última task session finalizada daquela máquina naquele job.
   É o mesmo que o console mostra no detalhe da sessão.

Ordem do status (a primeira que casar vence):

| Status | Quando |
|---|---|
| IGNORADO | Um admin marcou, com motivo obrigatório (fica auditado) |
| SEM BACKUP | Nenhum ponto, ou último ponto mais velho que o RPO |
| DESATIVADO | Job desativado no Veeam (ainda dentro do RPO) |
| FALHA | Último resultado *Failed*, ou ponto marcado como *Infected* |
| ALERTA | Último resultado *Warning*, ponto *Suspicious* ou sem sessão no histórico |
| OK | Último resultado *Success* (ou agente com ponto recente) |

Cada máquina tem a tela **"Por que este status"**, com as datas e a mensagem exata do Veeam.

### Como o sistema trata os casos que confundiam o sistema antigo

- **Retry**: o Veeam reprocessa só as VMs que falharam. Aqui vale a tarefa mais recente *de cada VM*,
  então quem deu certo na 1ª tentativa continua OK e quem falhou no retry continua FALHA.
- **Falha no nível do job** (ex.: *"backup meta file is not synchronized with the DB"*): o job morre
  antes de criar tarefas. Se essa sessão é a mais nova, o resultado dela vale para todas as VMs do job.
- **Job com Host/pasta inteira**: as VMs são descobertas pelas tarefas e pelos pontos de restauração.
- **Job encadeado** ("após o job X"): não tem `nextRun`, e isso é normal. Quem detecta atraso é o RPO.
- **Job em execução**: o Veeam informa `lastResult = None`. Aqui continua aparecendo o último resultado
  finalizado, mais o selo "job rodando".
- **VM em dois jobs**: aparece duas vezes, uma por job, como no console.
- **VM removida do job / backup órfão**: aparece enquanto tiver pontos, com o aviso "não está mais na
  lista do job". Se for intencional, o admin marca como ignorado.

### Garantias de confiabilidade

- **Tudo ou nada**: cada sincronização lê todos os endpoints essenciais (`jobs`, `jobs/states`,
  `sessions`, `taskSessions`, `backups`, `backupObjects`, `restorePoints`). Se qualquer um falhar,
  nada muda no banco e a falha fica registrada em *Diagnóstico*.
- **Paginação conferida**: o número de itens recebidos precisa bater com o `pagination.total` do Veeam.
  Se não bater, a leitura é repetida; se continuar diferente, a sincronização falha.
- **Checagens de sanidade**: ter sessões concluídas sem nenhuma task session, ou o inventário
  sumir de repente, derruba a sincronização em vez de mostrar dado errado.
- **Sem dado simulado**: não existe modo mock nem fallback em produção.
- **Status calculado na hora**: a idade do último ponto é recalculada a cada carregamento de página.
  Se a sincronização parar, os itens viram SEM BACKUP sozinhos, e uma faixa vermelha
  **DADOS DESATUALIZADOS** aparece em todas as telas.
- **Infraestrutura**: o backup da configuração do próprio Veeam, repositórios com menos de 10% livres
  ou offline e servidores indisponíveis aparecem no topo do painel.

### Limitações (da API do Veeam, não do sistema)

- **Veeam Agent standalone** (as estações da CBHidro etc.): o VBR 12.3 não expõe as sessões desses
  agentes na REST API. O status deles é avaliado **só pelo ponto de restauração** (padrão: 72 h).
  Um agente que roda com *warning* aparece como OK, desde que gere ponto.
- **IP das VMs**: a API não informa o IP do sistema operacional. O sistema mostra o host ESXi.

## Instalação

Requer Python 3.10+.

```bash
cd veeam-monitor
python -m venv .venv
# Linux:   source .venv/bin/activate
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
# edite o .env: SECRET_KEY, VEEAM_URL, VEEAM_USER, VEEAM_PASSWORD
python manage.py criar-usuario admin --admin
```

No Veeam, use um usuário com o papel **Veeam Backup Viewer**, que é somente leitura.

## Homologação (faça antes de confiar)

```bash
python manage.py testar-conexao     # login + confirma se /taskSessions existe nesta versão
python manage.py verificar --todos  # sincroniza e lista job por job, máquina por máquina
```

Abra o console do Veeam ao lado e confira:

1. **Home › Jobs**: status, último resultado e próxima execução de cada job devem bater com a seção JOBS.
2. Para 2 ou 3 jobs, abra a última sessão (*Statistics*) e confira o resultado de cada VM.
3. Em **Backups › Disk**, confira o último ponto de algumas máquinas, incluindo agentes.

Se algo divergir, gere um dump e analise:

```bash
python manage.py dump data/dump_divergencia
```

O dump contém nomes de clientes e máquinas. Não publique.

## Produção

```bash
python manage.py servir      # porta definida em APP_PORT (padrão 8000)
```

- **Windows Server**: `deploy/instalar-servico-windows.ps1` (usa NSSM).
- **Linux**: `deploy/veeam-monitor.service` (systemd).
- Rode **uma única instância**: a sincronização automática vive dentro do processo.
- Para monitorar o próprio monitor (Zabbix etc.): `GET /healthz` responde **200** se os dados estão
  atualizados e **503** se não estão. Não exige login nem expõe dados.
- Backup do sistema: copie `data/monitor.db` (contém usuários, clientes, RPOs, ignorados e histórico).

## Primeiro dia de uso

Na primeira sincronização devem aparecer muitos itens SEM BACKUP antigos: estações desligadas,
backups de jobs apagados, cadeias antigas de agentes. Isso é real, está no Veeam. Para cada um:

- se a máquina **devia** estar com backup, resolva no Veeam;
- se não devia, entre no item e marque **Ignorar**, com o motivo.

Depois disso, qualquer vermelho novo no painel é um problema real.

Ajuste também:
- **Clientes**: regras de agrupamento por texto no nome do job ou da máquina.
- **RPO por job**: jobs semanais ou mensais precisam de RPO maior (tela do job).

## Comandos

| Comando | O que faz |
|---|---|
| `python manage.py servir` | Sobe a web e a sincronização automática |
| `python manage.py criar-usuario NOME [--admin]` | Cria usuário (técnico ou admin) |
| `python manage.py senha NOME` | Troca a senha |
| `python manage.py testar-conexao` | Testa o acesso à API |
| `python manage.py sync` | Roda uma sincronização |
| `python manage.py verificar [--todos]` | Sincroniza e imprime tudo, para conferir com o console |
| `python manage.py dump PASTA` | Grava as respostas brutas da API |

## Testes

```bash
python -m pytest -q
```

Os testes cobrem retry, falha no nível do job, host inteiro, RPO e precedência, job em execução,
agente, órfão, desativado, ignorado, malware, paginação incompleta, sync que falha no meio
(retrato anterior preservado), endpoint auxiliar fora do ar, permissões e CSRF.
Se existir `data/replay_real/` (um dump real), o teste de invariantes confere que todo ponto de
restauração do Veeam vira uma linha no painel.

## Estrutura

```
app/
  veeam_client.py  cliente HTTP: login, paginação conferida, erros explícitos
  collector.py     coleta tudo numa passada (essenciais × auxiliares)
  analysis.py      dados brutos → fatos por máquina e por job (função pura)
  status.py        regra única de status (função pura, com o relógio atual)
  sync.py          sincronização tudo-ou-nada + agendador
  db.py            SQLite (histórico, retrato, metadados do admin, auditoria)
  views.py         junta retrato + metadados + status para as telas
  web.py           rotas, login, permissões
  templates/, static/   telas (CSS local, sem CDN: funciona sem internet)
manage.py          comandos de operação
tests/             testes automatizados
deploy/            serviço Windows (NSSM) e Linux (systemd)
```
