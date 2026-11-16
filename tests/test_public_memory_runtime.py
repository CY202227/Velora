import hashlib
import json
from pathlib import Path
import pytest
pytest.importorskip('atom_memory')
from scripts.memory_eval_runtime import restore_public_memory


def public_files(tmp_path):
    dataset=tmp_path/'conversation.json'
    dataset.write_text(json.dumps([{'sample_id':'public-test','conversation':{'speaker_a':'Alice','speaker_b':'Bob','session_1_date_time':'8 May 2023','session_1':[{'speaker':'Alice','dia_id':'D1:1','text':'I painted yesterday.'}]},'qa':[]}]),encoding='utf-8')
    snapshot=tmp_path/'snapshot.json'
    atom={'key':'painting','kind':'event','statement':'Alice painted yesterday.','detail':'Alice painted yesterday.','happened_on':None,'memory_layer':1,'status':'active','confidence':0.9,'current_source_ids':[1],'links':[]}
    snapshot.write_text(json.dumps({'configuration':{'dataset_sha256':hashlib.sha256(dataset.read_bytes()).hexdigest()},'diagnostics':[{'atoms':[atom],'source_statuses':[{'id':1,'status':'consolidated'}]}]}),encoding='utf-8')
    return dataset,snapshot


def test_public_snapshot_restores_original_evidence(tmp_path):
    dataset,snapshot=public_files(tmp_path)
    runtime=restore_public_memory(dataset,snapshot,object())
    try:
        detail=runtime.client.get(f'/spaces/{runtime.uid}/atoms/painting',params={'include':'evidence'}).json()
        assert [row['source_id'] for row in detail['evidence']]==[1]
        assert detail['confidence']==0.9
    finally:
        runtime.close()


def test_snapshot_cannot_attach_receipts_to_a_different_dataset(tmp_path):
    dataset,snapshot=public_files(tmp_path)
    dataset.write_text(dataset.read_text(encoding='utf-8')+'\n',encoding='utf-8')
    with pytest.raises(ValueError,match='exact original'):
        restore_public_memory(dataset,snapshot,object())


def test_public_source_chunking_preserves_evidence_ids():
    from types import SimpleNamespace
    import httpx
    from scripts.memory_eval_runtime import ingest_public_sources
    calls = []
    def handle(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"id": len(calls)})
    sample = SimpleNamespace(sample_id="public", conversation={
        "session_1_date_time": "8 May 2023",
        "session_1": [{"speaker": "Alice", "dia_id": f"D1:{i}", "text": f"fact-{i}"} for i in range(1, 8)],
    })
    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(handle)) as client:
        assert ingest_public_sources(client, "space", sample) == [1, 2]
    assert "fact-6" in calls[0]["content"] and "fact-7" not in calls[0]["content"]
    assert "fact-7" in calls[1]["content"]
    assert calls[1]["external_ref"]["chunk"] == 1
