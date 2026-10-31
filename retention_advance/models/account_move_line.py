from odoo import fields, models


class AccountMoveLine(models.Model):
    _inherit = 'account.move.line'

    # Both fields are copied on purpose: when Odoo reverses one of our entries the
    # reversal lines keep the link, so the report nets them out by itself.
    retention_origin_id = fields.Many2one(
        'account.move', string='Retention Invoice', index='btree_not_null', readonly=True,
        help="Invoice that generated this retention line.")
    retention_advance_payment_id = fields.Many2one(
        'account.payment', string='Advance Payment', index='btree_not_null', readonly=True,
        help="Advance payment recovered (or given back) by this line.")
