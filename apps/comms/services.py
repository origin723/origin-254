from django.core.exceptions import ValidationError
from django.db import transaction

from apps.core.exceptions import WorkflowError

from .models import Conversation, Message, Notification
from .notify import notify


@transaction.atomic
def start_conversation(user, other, subject, sourcing_request=None, order=None, first_message=""):
    """Buyer ↔ supplier (or admin) thread, reused if one already exists for the same context."""
    if other.pk == user.pk:
        raise ValidationError({"participant": "You cannot message yourself."})
    if not (user.is_platform_admin or other.is_platform_admin):
        if sourcing_request is None and order is None:
            raise WorkflowError("Conversations must be about a sourcing request or order.", "context_required")
        req = sourcing_request or order.sourcing_request
        buyer_id = req.buyer_id
        supplier_user_ids = set(req.matches.values_list("supplier__user_id", flat=True)) | set(
            req.quotations.values_list("supplier__user_id", flat=True))
        pair = {user.pk, other.pk}
        if buyer_id not in pair or not (pair - {buyer_id}) <= supplier_user_ids:
            raise WorkflowError("You can only contact suppliers matched to this request.", "permission_denied")
    convo = (Conversation.objects.filter(sourcing_request=sourcing_request, order=order, participants=user)
             .filter(participants=other).first())
    if convo is None:
        convo = Conversation.objects.create(subject=subject, sourcing_request=sourcing_request, order=order)
        convo.participants.add(user, other)
    if first_message:
        post_message(user, convo, first_message)
    return convo


@transaction.atomic
def post_message(user, conversation, body):
    if not conversation.participants.filter(pk=user.pk).exists():
        raise WorkflowError("You are not part of this conversation.", "permission_denied")
    body = (body or "").strip()
    if not body:
        raise ValidationError({"body": "Message cannot be empty."})
    message = Message.objects.create(conversation=conversation, sender=user, body=body)
    message.read_by.add(user)
    conversation.save(update_fields=["updated_at"])
    for other in conversation.participants.exclude(pk=user.pk):
        notify(other, f"New message from {user.display_name}", body[:140], Notification.Kind.MESSAGE,
               f"/app/messages/{conversation.pk}/")
    return message
