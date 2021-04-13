"""Define messages for credential holder admin protocols."""

# pylint: disable=invalid-name
# pylint: disable=too-few-public-methods

import re
from typing import Sequence, List, Optional, cast
import logging

from aries_cloudagent.config.injection_context import InjectionContext
from aries_cloudagent.connections.models.conn_record import ConnRecord
from aries_cloudagent.core.event_bus import Event, EventBus
from aries_cloudagent.core.profile import Profile, ProfileSession
from aries_cloudagent.core.protocol_registry import ProtocolRegistry
from aries_cloudagent.indy.holder import IndyHolder, IndyHolderError
from aries_cloudagent.ledger.error import LedgerError
from aries_cloudagent.wallet.error import WalletNotFoundError
from aries_cloudagent.messaging.agent_message import AgentMessage
from aries_cloudagent.messaging.base_handler import (
    BaseResponder, RequestContext
)
from aries_cloudagent.messaging.models.base import BaseModel, BaseModelError
from aries_cloudagent.storage.error import StorageError, StorageNotFoundError
from marshmallow import fields

from .. import ProblemReport
from ..decorators.pagination import Page, Paginate
from ..util import (
    ExceptionReporter, InvalidConnection, admin_only, expand_message_class,
    expand_model_class, get_connection, send_to_admins, with_generic_init,
    log_handling
)
from . import (
    CredentialAttributeSpec, CredentialManager, CredentialManagerError,
    CredentialProposalRequestSchema, CredExRecord, CredExRecordSchema,
    IndyCredPrecisSchema, PresentationPreview,
    PresentationProposalRequestSchema, PresExRecord, PresExRecordSchema,
    issue_credential, present_proof, PresentationManager
)


LOGGER = logging.getLogger(__name__)


@expand_model_class
class CredentialRepresentation(BaseModel):
    """Representation of Credentials in messages."""
    class Fields:
        """Fields for Credential Representation."""
        issuer_did = fields.Str()
        isser_connection_id = fields.Str()
        name = fields.Str()
        comment = fields.Str()
        received_at = fields.DateTime(format="iso")
        attributes = fields.List(fields.Nested(CredentialAttributeSpec))
        metadata = fields.Dict()
        raw_repr = fields.Dict()

    def __init__(
        self,
        *,
        issuer_did: str = None,
        issuer_connection_id: str = None,
        name: str = None,
        comment: str = None,
        received_at: str = None,
        attributes: Sequence[CredentialAttributeSpec] = None,
        metadata: dict = None,
        raw_repr: dict = None
    ):
        """Initialize model."""
        self.issuer_did = issuer_did
        self.issuer_connection_id = issuer_connection_id
        self.name = name
        self.comment = comment
        self.received_at = received_at
        self.attributes = attributes
        self.metadata = metadata
        self.raw_repr = raw_repr


class AdminHolderMessage(AgentMessage):
    """Admin Holder Protocol Message Base class."""
    protocol = 'did:sov:BzCbsNYhMrjHiqZDTUASHg;spec/admin-holder/0.1'


@expand_message_class
class CredGetList(AdminHolderMessage):
    """Credential list retrieval message."""
    message_type = "credentials-get-list"

    class Fields:
        """Credential get list fields."""
        paginate = fields.Nested(
            Paginate.Schema,
            required=False,
            data_key="~paginate",
            missing=Paginate(limit=10, offset=0),
        )
        states = fields.List(
            fields.Str(required=True),
            required=False,
            example=["offer_received"],
            description="Filter listed credentials by state.",
            validate=validate.OneOf(
                [
                    CredExRecord.STATE_ACKED,
                    CredExRecord.STATE_CREDENTIAL_RECEIVED,
                    CredExRecord.STATE_ISSUED,
                    CredExRecord.STATE_OFFER_RECEIVED,
                    CredExRecord.STATE_OFFER_SENT,
                    CredExRecord.STATE_PROPOSAL_RECEIVED,
                    CredExRecord.STATE_PROPOSAL_SENT,
                    CredExRecord.STATE_REQUEST_RECEIVED,
                    CredExRecord.STATE_REQUEST_SENT,
                ]
            ),
        )

    def __init__(
        self,
        paginate: Paginate = None,
        states: Optional[List[str]] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.paginate = paginate
        self.states = states

    @log_handling
    @admin_only
    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle received get cred list request."""
        session = await context.session()

        credentials = await CredExRecord.query(session)

        if self.states:
            credentials = [c for c in credentials if c.state in self.states]

        credentials, page = self.paginate.apply(credentials)

        cred_list = CredList(
            results=[credential.serialize() for credential in credentials],
            page=page
        )
        await responder.send_reply(cred_list)


@expand_message_class
class CredList(AdminHolderMessage):
    """Credential list message."""
    message_type = "credentials-list"

    class Fields:
        """Fields of credential list message."""
        results = fields.List(
            fields.Dict(),
            required=True,
            description="List of requested credentials",
            example=[],
        )
        page = fields.Nested(Page.Schema, required=False, data_key="~page")

    def __init__(
        self,
        results: Sequence[dict],
        page: Page = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.results = results
        self.page = page


@with_generic_init
@expand_message_class
class SendCredProposal(AdminHolderMessage):
    """Send Credential Proposal Message."""
    message_type = "send-credential-proposal"
    fields_from = CredentialProposalRequestSchema

    @log_handling
    @admin_only
    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle received send proposal request."""
        connection_id = str(context.message.connection_id)
        credential_definition_id = context.message.cred_def_id
        comment = context.message.comment

        credential_manager = CredentialManager(context.profile)

        session = await context.session()
        try:
            conn_record = await ConnRecord.retrieve_by_id(
                session,
                connection_id
            )
        except StorageNotFoundError:
            report = ProblemReport(
                explain_ltxt='Connection not found.',
                who_retries='none'
            )
            report.assign_thread_from(context.message)
            await responder.send_reply(report)
            return

        if not conn_record.is_ready:
            report = ProblemReport(
                explain_ltxt='Connection invalid.',
                who_retries='none'
            )
            report.assign_thread_from(context.message)
            await responder.send_reply(report)
            return

        credential_exchange_record = await credential_manager.create_proposal(
            connection_id,
            comment=comment,
            credential_preview=context.message.credential_proposal,
            cred_def_id=credential_definition_id
        )

        await responder.send(
            issue_credential.messages.credential_proposal.CredentialProposal(
                comment=context.message.comment,
                credential_proposal=context.message.credential_proposal,
                cred_def_id=credential_definition_id
            ),
            connection_id=connection_id
        )
        cred_exchange = CredExchange(**credential_exchange_record.serialize())
        cred_exchange.assign_thread_from(context.message)
        await responder.send_reply(cred_exchange)


@with_generic_init
@expand_message_class
class CredExchange(AdminHolderMessage):
    """Credential exchange message."""
    message_type = "credential-exchange"
    fields_from = CredExRecordSchema


@with_generic_init
@expand_message_class
class CredOfferRecv(AdminHolderMessage):
    """Credential offer received message."""
    message_type = "credential-offer-received"
    fields_from = CredExRecordSchema


@expand_message_class
class CredOfferAccept(AdminHolderMessage):
    """Credential offer accept message."""
    message_type = "credential-offer-accept"

    class Fields:
        """Fields of cred offer accept message."""
        credential_exchange_id = fields.Str(
            required=True,
            description="ID of the credential exchange to accept",
            example=UUIDFour.EXAMPLE
        )

    def __init__(self, credential_exchange_id: str = None, **kwargs):
        super().__init__(**kwargs)
        self.credential_exchange_id = credential_exchange_id

    @log_handling
    @admin_only
    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle credential offer accept message."""

        cred_ex_record = None
        connection_record = None
        async with context.session() as session:
            async with ExceptionReporter(
                responder,
                (StorageError, CredentialManagerError, BaseModelError),
                self
            ):
                cred_ex_record = await CredExRecord.retrieve_by_id(
                    session, self.credential_exchange_id
                )
                connection_id = cred_ex_record.connection_id
                connection_record = await get_connection(session, connection_id)

        credential_manager = CredentialManager(context.profile)
        (
            cred_ex_record,
            credential_request_message,
        ) = await credential_manager.create_request(
            cred_ex_record, connection_record.my_did
        )

        sent = CredRequestSent(**cred_ex_record.serialize())

        await responder.send(credential_request_message, connection_id=connection_id)
        await responder.send_reply(sent)


@with_generic_init
@expand_message_class
class CredRequestSent(AdminHolderMessage):
    """Credential offer acceptance received and credential request sent."""
    message_type = "credential-request-sent"
    fields_from = CredExRecordSchema


@with_generic_init
@expand_message_class
class CredReceived(AdminHolderMessage):
    """Credential received notification message."""
    message_type = "credential-received"
    fields_from = CredExRecordSchema


@expand_message_class
class PresGetList(AdminHolderMessage):
    """Presentation get list message."""
    message_type = 'presentations-get-list'

    class Fields:
        """Message fields."""
        connection_id = fields.Str(required=False)
        verified = fields.Str(required=False)
        paginate = fields.Nested(
            Paginate.Schema,
            required=False,
            data_key="~paginate",
            missing=Paginate(limit=10, offset=0)
        )

    def __init__(
        self,
        connection_id: str = None,
        verified: str = None,
        paginate: Paginate = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.connection_id = connection_id
        self.verified = verified
        self.paginate = paginate or Paginate()

    @log_handling
    @admin_only
    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle received get cred list request."""

        session = await context.session()
        paginate: Paginate = context.message.paginate

        post_filter_positive = dict(
            filter(lambda item: item[1] is not None, {
                'role': PresExRecord.ROLE_PROVER,
                'connection_id': context.message.connection_id,
                'verified': context.message.verified,
            }.items())
        )
        records = await PresExRecord.query(
            session, {}, post_filter_positive=post_filter_positive
        )
        records, page = paginate.apply(records)
        pres_list = PresList([record.serialize() for record in records], page=page)
        await responder.send_reply(pres_list)


@expand_message_class
class PresList(AdminHolderMessage):
    """Presentation get list response message."""
    message_type = 'presentations-list'

    class Fields:
        """Fields for presentation list message."""
        results = fields.List(fields.Dict())
        page = fields.Nested(Page.Schema, required=False, data_key="~page")

    def __init__(self, results, page: Page = None, **kwargs):
        super().__init__(**kwargs)
        self.results = results
        self.page = page


@expand_message_class
class SendPresProposal(AdminHolderMessage):
    """Presentation proposal message."""
    message_type = 'send-presentation-proposal'
    fields_from = PresentationProposalRequestSchema

    def __init__(
        self,
        *,
        connection_id: str = None,
        comment: str = None,
        presentation_proposal: PresentationPreview = None,
        auto_present: bool = None,
        trace: bool = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.connection_id = connection_id
        self.comment = comment
        self.presentation_proposal = presentation_proposal
        self.auto_present = auto_present
        self.trace = trace

    @log_handling
    @admin_only
    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle received send presentation proposal request."""
        session = await context.session()
        connection_id = str(context.message.connection_id)
        async with ExceptionReporter(responder, InvalidConnection, context.message):
            await get_connection(session, connection_id)

        comment = context.message.comment
        # Aries#0037 calls it a proposal in the proposal struct but it's of type preview
        presentation_proposal = present_proof.messages.presentation_proposal.PresentationProposal(
            comment=comment,
            presentation_proposal=context.message.presentation_proposal
        )
        auto_present = (
            context.message.auto_present or
            context.settings.get("debug.auto_respond_presentation_request")
        )

        presentation_manager = present_proof.manager.PresentationManager(context.profile)

        presentation_exchange_record = (
            await presentation_manager.create_exchange_for_proposal(
                connection_id=connection_id,
                presentation_proposal_message=presentation_proposal,
                auto_present=auto_present
            )
        )
        await responder.send(presentation_proposal, connection_id=connection_id)

        pres_exchange = PresExchange(**presentation_exchange_record.serialize())
        pres_exchange.assign_thread_from(context.message)
        await responder.send_reply(pres_exchange)


@with_generic_init
@expand_message_class
class PresExchange(AdminHolderMessage):
    """Presentation Exchange message."""
    message_type = "presentation-exchange"
    fields_from = PresExRecordSchema


@expand_message_class
class PresRequestReceived(AdminHolderMessage):
    """Presentation Request Received."""
    message_type = "presentation-request-received"

    DEFAULT_COUNT = 10

    class Fields:
        """Fields of Presentation request received message."""
        record = fields.Nested(PresExRecordSchema)
        matching_credentials = fields.Nested(IndyCredPrecisSchema, many=True)
        page = fields.Nested(Page.Schema, required=False)

    def __init__(self, record: PresExRecord, **kwargs):
        super().__init__(**kwargs)
        self.record = record
        self.matching_credentials = []
        self.page = None

    async def retrieve_matching_credentials(self, profile: Profile):
        holder = profile.inject(IndyHolder)
        self.matching_credentials = await holder.get_credentials_for_presentation_request_by_referent(
            self.record.presentation_request,
            (),
            0,
            self.DEFAULT_COUNT,
            extra_query={},
        )
        self.page = Page(count_=self.DEFAULT_COUNT, offset=self.DEFAULT_COUNT)


class InvalidPresentationExchange(Exception):
    """Raised when given Presentation Exchange ID or record is not valid."""


@expand_message_class
class PresRequestApprove(AdminHolderMessage):
    """Approve presentation request."""
    message_type = "presentation-request-approve"

    class Fields:
        """Fields on pres request approve message."""
        presentation_exchange_id = fields.Str(required=True)
        self_attested_attributes = fields.Dict(
            description="Self-attested attributes to build into proof",
            required=True,
            keys=fields.Str(example="attr_name"),  # marshmallow/apispec v3.0 ignores
            values=fields.Str(
                example="self_attested_value",
                description=(
                    "Self-attested attribute values to use in requested-credentials "
                    "structure for proof construction"
                ),
            ),
        )
        requested_attributes = fields.Dict(
            description=(
                "Nested object mapping proof request attribute referents to "
                "requested-attribute specifiers"
            ),
            required=True,
            keys=fields.Str(example="attr_referent"),  # marshmallow/apispec v3.0 ignores
            values=fields.Nested(present_proof.routes.IndyRequestedCredsRequestedAttrSchema()),
        )
        requested_predicates = fields.Dict(
            description=(
                "Nested object mapping proof request predicate referents to "
                "requested-predicate specifiers"
            ),
            required=True,
            keys=fields.Str(example="pred_referent"),  # marshmallow/apispec v3.0 ignores
            values=fields.Nested(present_proof.routes.IndyRequestedCredsRequestedPredSchema()),
        )
        comment = fields.Str(required=False)

    def __init__(
        self,
        presentation_exchange_id: str,
        self_attested_attributes: dict,
        requested_attributes: dict,
        requested_predicates: dict,
        comment: str = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self.presentation_exchange_id = presentation_exchange_id
        self.self_attested_attributes = self_attested_attributes
        self.requested_attributes = requested_attributes
        self.requested_predicates = requested_predicates
        self.comment = comment

    @staticmethod
    async def get_pres_ex_record(
        session: ProfileSession, pres_ex_id: str
    ) -> PresExRecord:
        """Retrieve a presentation exchange record and validate its state."""
        try:
            pres_ex_record = await PresExRecord.retrieve_by_id(
                session, pres_ex_id
            )
            pres_ex_record = cast(PresExRecord, pres_ex_record)
        except StorageNotFoundError as err:
            raise InvalidPresentationExchange(
                "Presentation exchange ID not found"
            ) from err

        if pres_ex_record.state != (PresExRecord.STATE_REQUEST_RECEIVED):
            raise InvalidPresentationExchange(
                "Presentation must be in request received state"
            )

        return pres_ex_record

    @log_handling
    @admin_only
    async def handle(self, context: RequestContext, responder: BaseResponder):
        """Handle presentation request approved message."""
        async with context.session() as session:
            async with ExceptionReporter(
                responder, InvalidPresentationExchange, context.message
            ):
                pres_ex_record = await self.get_pres_ex_record(
                    session, self.presentation_exchange_id
                )

            async with ExceptionReporter(
                responder, InvalidConnection, context.message
            ):
                conn_record = await get_connection(
                    session, pres_ex_record.connection_id
                )

        presentation_manager = PresentationManager(context.profile)
        async with ExceptionReporter(
            responder,
            (
                BaseModelError,
                IndyHolderError,
                LedgerError,
                StorageError,
                WalletNotFoundError
            ),
            context.message
        ):
            pres_ex_record, message = await presentation_manager.create_presentation(
                pres_ex_record,
                {
                    "self_attested_attributes": self.self_attested_attributes,
                    "requested_attributes": self.requested_attributes,
                    "requested_predicates": self.requested_predicates
                },
                comment=self.comment
            )

        await responder.send(message, connection_id=conn_record.connection_id)

        presentation_sent = PresSent(**pres_ex_record.serialize())
        presentation_sent.assign_thread_from(self)
        await responder.send_reply(presentation_sent)


@with_generic_init
@expand_message_class
class PresSent(AdminHolderMessage):
    """Presentation Exchange message."""
    message_type = "presentation-sent"
    fields_from = PresExRecordSchema


PROTOCOL = AdminHolderMessage.protocol
TITLE = "Holder Admin Protocol"
NAME = "admin-holder"
VERSION = "0.1"
MESSAGE_TYPES = {
    msg_class.Meta.message_type: '{}.{}'.format(msg_class.__module__, msg_class.__name__)
    for msg_class in [
        CredGetList,
        CredList,
        CredOfferAccept,
        CredOfferRecv,
        CredRequestSent,
        CredReceived,
        SendCredProposal,
        CredExchange,
        PresGetList,
        PresList,
        PresRequestApprove,
        SendPresProposal,
        PresExchange,
    ]
}


async def setup(
        context: InjectionContext,
        protocol_registry: ProblemReport = None
):
    """Setup the holder plugin."""
    if not protocol_registry:
        protocol_registry = context.inject(ProtocolRegistry)
    protocol_registry.register_message_types(
        MESSAGE_TYPES
    )
    bus: EventBus = context.inject(EventBus)
    bus.subscribe(
        re.compile(CredExRecord.WEBHOOK_TOPIC + ".*"),
        issue_credential_event_handler
    )
    bus.subscribe(
        re.compile(PresExRecord.WEBHOOK_TOPIC + ".*"),
        present_proof_event_handler
    )


async def issue_credential_event_handler(profile: Profile, event: Event):
    """Handle issue credential events."""
    record: CredExRecord = CredExRecord.deserialize(event.payload)
    LOGGER.debug("IssueCredential Event; %s: %s", event.topic, event.payload)

    if record.state not in (
        CredExRecord.STATE_OFFER_RECEIVED,
        CredExRecord.STATE_CREDENTIAL_RECEIVED
    ):
        return

    responder = profile.inject(BaseResponder)
    message = None
    if record.state == CredExRecord.STATE_OFFER_RECEIVED:
        message = CredOfferRecv(**record.serialize())
        LOGGER.debug("Prepared Message: %s", message.serialize())

    if record.state == CredExRecord.STATE_CREDENTIAL_RECEIVED:
        message = CredReceived(**record.serialize())
        LOGGER.debug("Prepared Message: %s", message.serialize())

    async with profile.session() as session:
        await send_to_admins(
            session,
            message,
            responder
        )


async def present_proof_event_handler(profile: Profile, event: Event):
    """Handle present proof events."""
    record: PresExRecord = PresExRecord.deserialize(event.payload)
    LOGGER.debug("PresentProof Event; %s: %s", event.topic, event.payload)

    if record.state == PresExRecord.STATE_REQUEST_RECEIVED:
        responder = profile.inject(BaseResponder)
        message = PresRequestReceived(record)
        LOGGER.debug("Prepared Message: %s", message.serialize())
        await message.retrieve_matching_credentials(profile)
        async with profile.session() as session:
            await send_to_admins(session, message, responder)
