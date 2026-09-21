"""Exclusão de uma thread pelo painel: move para a lixeira, nunca apaga."""
import http.client
import json
import threading
from http.server import ThreadingHTTPServer

import pytest

from src.squad import painel


def _gravar(diretorio, tid, desfecho="deploy"):
    eventos = [
        {"evento": "inicio_execucao", "quando": "2026-09-21T10:00:00+00:00", "pedido": "p"},
        {"evento": "planejamento", "quando": "2026-09-21T10:01:00+00:00",
         "inicio": "2026-09-21T10:00:00+00:00", "duracao_s": 60.0},
    ]
    if desfecho != "em_curso":
        eventos.append({"evento": "fim_execucao", "quando": "2026-09-21T10:02:00+00:00",
                        "desfecho": desfecho})
    (diretorio / f"{tid}.json").write_text(json.dumps(eventos), encoding="utf-8")


@pytest.fixture
def ambiente(tmp_path):
    painel._LINHAS.clear()
    metrics, workspace = tmp_path / "metrics", tmp_path / "workspace"
    metrics.mkdir()
    (workspace / "t1" / "app").mkdir(parents=True)
    (workspace / "t1" / "app" / "page.tsx").write_text("x")
    _gravar(metrics, "t1")
    _gravar(metrics, "t2")
    return metrics, workspace


def test_metricas_e_workspace_vao_para_a_lixeira(ambiente):
    metrics, workspace = ambiente
    r = painel.excluir_execucao(metrics, "t1", workspace)
    assert not (metrics / "t1.json").exists() and not (workspace / "t1").exists()
    assert len(list((metrics / "lixeira").glob("t1.*.json"))) == 1
    guardado = next((workspace / ".lixeira").glob("t1.*"))
    assert (guardado / "app" / "page.tsx").read_text() == "x"  # recuperável
    assert len(r["movidos"]) == 2
    assert [linha["thread_id"] for linha in painel.listar_execucoes(metrics)] == ["t2"]


def test_ramos_da_execucao_paralela_vao_junto(ambiente):
    metrics, workspace = ambiente
    _gravar(metrics, "t1--api")
    (workspace / "t1--api").mkdir()
    painel.excluir_execucao(metrics, "t1", workspace)
    assert not (metrics / "t1--api.json").exists() and not (workspace / "t1--api").exists()
    assert (metrics / "t2.json").exists()


def test_execucao_em_curso_nao_sai(ambiente):
    metrics, workspace = ambiente
    _gravar(metrics, "viva", desfecho="em_curso")
    with pytest.raises(painel.ExclusaoRecusada, match="em curso"):
        painel.excluir_execucao(metrics, "viva", workspace)
    assert (metrics / "viva.json").exists()


def test_thread_sem_workspace_so_move_as_metricas(ambiente):
    metrics, workspace = ambiente
    r = painel.excluir_execucao(metrics, "t2", workspace)
    assert len(r["movidos"]) == 1


# ---------- a rota HTTP ----------

@pytest.fixture
def servidor(ambiente, monkeypatch):
    metrics, workspace = ambiente
    monkeypatch.setattr(painel, "DIR_WORKSPACE", workspace)
    monkeypatch.setattr(painel._Painel, "diretorio", metrics)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), painel._Painel)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv.server_address[1], metrics
    srv.shutdown()
    srv.server_close()


def _delete(porta, caminho, cabecalhos):
    conexao = http.client.HTTPConnection("127.0.0.1", porta, timeout=5)
    conexao.request("DELETE", caminho, headers=cabecalhos)
    resposta = conexao.getresponse()
    return resposta.status, json.loads(resposta.read() or b"{}")


def test_rota_exclui_com_o_cabecalho_do_painel(servidor):
    porta, metrics = servidor
    status, corpo = _delete(porta, "/api/execucao/t1", {"X-Painel": "1"})
    assert status == 200 and corpo["thread_id"] == "t1"
    assert not (metrics / "t1.json").exists()


def test_rota_recusa_chamada_de_outro_site(servidor):
    """Sem o cabeçalho próprio — o que um formulário ou fetch simples de
    outra página consegue mandar —, ou com outra origem, nada sai."""
    porta, metrics = servidor
    assert _delete(porta, "/api/execucao/t1", {})[0] == 403
    assert _delete(porta, "/api/execucao/t1",
                   {"X-Painel": "1", "Origin": "http://evil.example"})[0] == 403
    assert (metrics / "t1.json").exists()


def test_rota_recusa_id_desconhecido_e_travessia(servidor):
    porta, _ = servidor
    assert _delete(porta, "/api/execucao/nao-existe", {"X-Painel": "1"})[0] == 404
    assert _delete(porta, "/api/execucao/..%2F..%2Fsegredo", {"X-Painel": "1"})[0] == 404
