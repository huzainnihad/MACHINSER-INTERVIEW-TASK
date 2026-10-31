from odoo import _, api, fields, models
from odoo.exceptions import UserError


class SaleOrder(models.Model):
    _inherit = 'sale.order'

    advance_payment_ids = fields.One2many(
        'account.payment', 'advance_sale_order_id', string='Advance Payments', readonly=True)
    advance_payment_count = fields.Integer(
        string='Advance Payments', compute='_compute_advance_payment_count')

    @api.depends('advance_payment_ids', 'advance_payment_ids.is_advance')
    def _compute_advance_payment_count(self):
        for order in self:
            order.advance_payment_count = len(order.advance_payment_ids.filtered('is_advance'))

    def _advance_payment_action_context(self):
        self.ensure_one()
        # The payment form selects available bank journals from env.company.
        # Put the order's company first even when another company is active.
        active_companies = self.env.context.get('allowed_company_ids', self.env.companies.ids)
        return {
            'allowed_company_ids': [self.company_id.id] + [
                company_id for company_id in active_companies if company_id != self.company_id.id],
            'default_payment_type': 'inbound',
            'default_partner_type': 'customer',
            'default_partner_id': self.partner_id.commercial_partner_id.id,
            'default_company_id': self.company_id.id,
            'default_is_advance': True,
            'default_advance_sale_order_id': self.id,
            'default_move_journal_types': ('bank', 'cash'),
        }

    def action_record_advance_payment(self):
        self.ensure_one()
        if self.state != 'sale':
            raise UserError(_('Confirm the sales order before recording an advance payment.'))
        return {
            'type': 'ir.actions.act_window',
            'name': _('Advance Payment'),
            'res_model': 'account.payment',
            'view_mode': 'form',
            'views': [(self.env.ref('account.view_account_payment_form').id, 'form')],
            'target': 'current',
            'context': self._advance_payment_action_context(),
        }

    def action_view_advance_payments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _('Advance Payments'),
            'res_model': 'account.payment',
            'view_mode': 'list,form',
            'domain': [('advance_sale_order_id', '=', self.id), ('is_advance', '=', True)],
            'context': self._advance_payment_action_context(),
        }
