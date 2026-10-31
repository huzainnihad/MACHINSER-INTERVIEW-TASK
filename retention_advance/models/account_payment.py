from odoo import _, api, fields, models
from odoo.exceptions import ValidationError
from odoo.tools import SQL


class AccountPayment(models.Model):
    _inherit = 'account.payment'

    is_advance = fields.Boolean(
        string='Advance Payment',
        help="Tick for a customer payment received upfront without an invoice. "
             "It can then be recovered from progress invoices.",
    )
    advance_sale_order_id = fields.Many2one(
        'sale.order', string='Project (Sales Order)', check_company=True,
        help="The project this advance belongs to. Linked advances cannot exceed the sales order total.",
    )

    def _advance_amount_in_order_currency(self):
        self.ensure_one()
        order = self.advance_sale_order_id
        return self.currency_id._convert(
            self.amount, order.currency_id, order.company_id, self.date)

    def _check_advance_order_limit(self, posting=False):
        """Limit all received advances for an order to its total, including VAT.

        Drafts do not reserve the order value; a second check at posting catches
        drafts saved earlier or posted together.
        """
        candidates = self.filtered(lambda p: p.is_advance and p.advance_sale_order_id and p.amount > 0)
        for order in candidates.mapped('advance_sale_order_id'):
            selected = candidates.filtered(lambda p: p.advance_sale_order_id == order)
            for payment in selected:
                if payment.payment_type != 'inbound' or payment.partner_type != 'customer':
                    raise ValidationError(_("A project advance must be an inbound customer payment."))
                if payment.company_id != order.company_id or \
                        payment.partner_id.commercial_partner_id != order.partner_id.commercial_partner_id:
                    raise ValidationError(_(
                        "The advance payment must belong to the sales order's company and customer."))

            received = self.search([
                ('advance_sale_order_id', '=', order.id),
                ('is_advance', '=', True),
                ('payment_type', '=', 'inbound'),
                ('state', 'in', ('in_process', 'paid')),
                ('id', 'not in', selected.ids),
            ])
            previous = sum(payment._advance_amount_in_order_currency() for payment in received)
            if posting:
                proposed = sum(payment._advance_amount_in_order_currency() for payment in selected)
            else:
                # Saving several drafts does not reserve the same order balance.
                # Each draft is checked against advances that were already posted.
                proposed = max(payment._advance_amount_in_order_currency() for payment in selected)
            currency = order.currency_id
            if currency.compare_amounts(previous + proposed, order.amount_total) > 0:
                raise ValidationError(_(
                    "The advance exceeds sales order %(order)s. Order total: %(total)s; "
                    "already received: %(received)s; maximum new advance: %(available)s.",
                    order=order.name,
                    total=currency.format(order.amount_total),
                    received=currency.format(previous),
                    available=currency.format(max(order.amount_total - previous, 0.0)),
                ))

    @api.constrains('is_advance', 'advance_sale_order_id', 'amount', 'currency_id',
                    'date', 'company_id', 'partner_id', 'payment_type', 'partner_type')
    def _constrain_advance_order_limit(self):
        self._check_advance_order_limit()

    def action_post(self):
        drafts = self.filtered(lambda p: p.state == 'draft')
        order_ids = sorted(drafts.filtered(
            lambda p: p.is_advance and p.advance_sale_order_id).advance_sale_order_id.ids)
        if order_ids:
            # Serialize concurrent postings for the same project. The second
            # transaction sees the first payment after its order lock is released.
            self.env.execute_query(SQL(
                "SELECT id FROM sale_order WHERE id = ANY(%s) ORDER BY id FOR UPDATE", order_ids))
        drafts._check_advance_order_limit(posting=True)
        # With Accounting installed Odoo may leave a payment without journal entry until a bank statement
        # is matched. An advance must be a posted entry (it is the credit that invoices recover from).
        for payment in self.filtered(lambda p: p.is_advance and not p.move_id and not p.outstanding_account_id):
            payment.outstanding_account_id = payment._get_outstanding_account(payment.payment_type)
        return super().action_post()

    def _get_advance_line(self):
        """The receivable journal item of the payment, i.e. the open credit that is consumed by recoveries."""
        self.ensure_one()
        return self.move_id.line_ids.filtered(
            lambda line: line.account_id.account_type == 'asset_receivable')

    def _get_advance_remaining(self, currency, date):
        """What is left of the advance, expressed in `currency`."""
        self.ensure_one()
        line = self._get_advance_line()
        remaining = -sum(line.mapped('amount_residual_currency'))
        if line.currency_id != currency:
            remaining = line.currency_id._convert(remaining, currency, self.company_id, date)
        return max(remaining, 0.0)
