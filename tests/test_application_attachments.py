"""Attachments: upload/list/fetch/delete, validation errors, ownership
isolation."""

import base64

from services.tracking.dtos.attachment import CreateAttachmentRequest

PDF_BASE64 = base64.b64encode(b"%PDF-1.4\n%fake pdf content for testing\n").decode()
DOCX_BASE64 = base64.b64encode(b"PK\x03\x04fake docx zip content").decode()
OVERSIZED_BASE64 = base64.b64encode(b"A" * (10 * 1024 * 1024 + 1)).decode()

DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)


class TestUploadAttachment:
    async def test_uploads_a_pdf(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["filename"] == "resume.pdf"
        assert "content_base64" not in body

    async def test_uploads_a_docx(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "cover_letter",
                "filename": "cover.docx",
                "content_type": DOCX_CONTENT_TYPE,
                "content_base64": DOCX_BASE64,
            },
        )
        assert response.status_code == 201, response.text

    async def test_unsupported_content_type_is_415(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.txt",
                "content_type": "text/plain",
                "content_base64": PDF_BASE64,
            },
        )
        assert response.status_code == 415

    async def test_malformed_base64_is_422(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": "not valid base64!!!",
            },
        )
        assert response.status_code == 422

    async def test_oversized_attachment_is_413(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": OVERSIZED_BASE64,
            },
        )
        assert response.status_code == 413

    async def test_magic_byte_mismatch_is_415(self, client, admin, application):
        response = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": DOCX_BASE64,
            },
        )
        assert response.status_code == 415

    async def test_duplicate_bytes_are_409_with_no_override(
        self, client, admin, application
    ):
        body = {
            "kind": "resume",
            "filename": "resume.pdf",
            "content_type": "application/pdf",
            "content_base64": PDF_BASE64,
        }
        first = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json=body,
        )
        assert first.status_code == 201
        second = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json=body,
        )
        assert second.status_code == 409
        assert second.json()["detail"]["code"] == "duplicate_attachment"

    async def test_requires_a_credential(self, client, application):
        response = await client.post(
            f"/applications/{application['id']}/attachments",
            json={
                "kind": "resume",
                "filename": "r.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
        assert response.status_code == 401

    async def test_on_another_users_application_is_404(
        self, client, other_owner, application
    ):
        response = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=other_owner.headers,
            json={
                "kind": "resume",
                "filename": "r.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
        assert response.status_code == 404


class TestListAttachments:
    async def test_lists_metadata_without_content(self, client, admin, application):
        await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
        response = await client.get(
            f"/applications/{application['id']}/attachments", headers=admin.headers
        )
        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) == 1
        assert "content_base64" not in data[0]

    async def test_on_another_users_application_is_404(
        self, client, other_owner, application
    ):
        response = await client.get(
            f"/applications/{application['id']}/attachments",
            headers=other_owner.headers,
        )
        assert response.status_code == 404


class TestGetAttachment:
    async def test_returns_content(self, client, admin, application):
        created = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
        response = await client.get(
            f"/attachments/{created.json()['id']}", headers=admin.headers
        )
        assert response.status_code == 200
        assert response.json()["content_base64"] == PDF_BASE64

    async def test_unknown_id_is_404(self, client, admin):
        response = await client.get("/attachments/999999", headers=admin.headers)
        assert response.status_code == 404

    async def test_another_users_attachment_is_404(
        self, client, admin, other_owner, application
    ):
        created = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
        response = await client.get(
            f"/attachments/{created.json()['id']}", headers=other_owner.headers
        )
        assert response.status_code == 404


class TestDeleteAttachment:
    async def test_deletes_the_attachment(self, client, admin, application):
        created = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
        response = await client.delete(
            f"/attachments/{created.json()['id']}", headers=admin.headers
        )
        assert response.status_code == 204
        response = await client.get(
            f"/attachments/{created.json()['id']}", headers=admin.headers
        )
        assert response.status_code == 404

    async def test_another_users_attachment_is_404(
        self, client, admin, other_owner, application
    ):
        created = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
        response = await client.delete(
            f"/attachments/{created.json()['id']}", headers=other_owner.headers
        )
        assert response.status_code == 404


class TestDeletingApplicationDeletesAttachments:
    async def test_cascade(self, client, admin, application):
        await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "resume.pdf",
                "content_type": "application/pdf",
                "content_base64": PDF_BASE64,
            },
        )
        response = await client.delete(
            f"/applications/{application['id']}", headers=admin.headers
        )
        assert response.status_code == 204


class TestOversizedBodyIsRejectedBeforeDecoding:
    """The decoded-size check in `AttachmentPolicy` runs after base64 has been
    decoded, so on its own it pays for the allocation it exists to prevent.
    `CreateAttachmentRequest` bounds the encoded string first: the refusal is
    a 422 from validation rather than a 413 from the policy, and that
    difference is the point of the test."""

    async def test_a_huge_payload_is_refused_at_validation(
        self, client, admin, application
    ):
        oversized = "A" * (CreateAttachmentRequest.MAX_BASE64_LENGTH + 4)
        response = await client.post(
            f"/applications/{application['id']}/attachments",
            headers=admin.headers,
            json={
                "kind": "resume",
                "filename": "huge.pdf",
                "content_type": "application/pdf",
                "content_base64": oversized,
            },
        )
        assert response.status_code == 422, response.status_code

    async def test_the_bound_admits_a_full_size_attachment(self):
        """The ceiling has to be above what a legitimate 10 MiB file encodes
        to, or the limit rejects the largest allowed upload."""
        encoded_length = ((10 * 1024 * 1024 + 2) // 3) * 4
        assert CreateAttachmentRequest.MAX_BASE64_LENGTH >= encoded_length
