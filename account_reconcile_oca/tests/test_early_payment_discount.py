from freezegun import freeze_time

from odoo import Command, fields
from odoo.tests import Form, tagged

from .test_bank_account_reconcile import TestAccountReconciliationCommon


@tagged("post_install", "-at_install")
class TestEarlyPaymentDiscount(TestAccountReconciliationCommon):
    def _create_epd_payment_term(self, discount_percentage=10.0):
        return self.env["account.payment.term"].create(
            {
                "name": f"{discount_percentage:.0f}% discount if paid within 10 days",
                "early_discount": True,
                "discount_percentage": discount_percentage,
                "discount_days": 10,
                "line_ids": [
                    Command.create(
                        {
                            "value": "percent",
                            "value_amount": 100,
                            "nb_days": 30,
                        }
                    )
                ],
            }
        )

    def _create_invoice_matching_model(self, partner, auto_reconcile=False):
        return self.env["account.reconcile.model"].create(
            {
                "name": "EPD invoice matching",
                "sequence": 1,
                "rule_type": "invoice_matching",
                "match_partner": True,
                "match_partner_ids": [Command.set(partner.ids)],
                "match_text_location_label": True,
                "match_text_location_reference": False,
                "match_text_location_note": False,
                "allow_payment_tolerance": True,
                "payment_tolerance_type": "percentage",
                "payment_tolerance_param": 0.0,
                "match_same_currency": True,
                "past_months_limit": False,
                "company_id": self.company.id,
                "auto_reconcile": auto_reconcile,
            }
        )

    def _create_statement_line(
        self,
        amount,
        payment_ref,
        partner,
        date="2019-01-10",
        amount_currency=None,
        foreign_currency_id=None,
    ):
        bank_stmt = self.acc_bank_stmt_model.create(
            {
                "journal_id": self.bank_journal_euro.id,
                "date": date,
                "name": "epd-test",
            }
        )
        line_vals = {
            "name": "epd-line",
            "journal_id": self.bank_journal_euro.id,
            "statement_id": bank_stmt.id,
            "amount": amount,
            "date": date,
            "payment_ref": payment_ref,
            "partner_id": partner.id,
        }
        if amount_currency is not None:
            line_vals["amount_currency"] = amount_currency
        if foreign_currency_id:
            line_vals["foreign_currency_id"] = foreign_currency_id
        return self.acc_bank_stmt_line_model.create(line_vals)

    def _get_receivable_or_payable_line(self, invoice):
        line = invoice.line_ids.filtered(
            lambda record: record.account_id.account_type
            in ("asset_receivable", "liability_payable")
        )
        self.assertEqual(len(line), 1)
        return line

    def _get_counterpart_lines(self, reconcile_data, move_line):
        return [
            line
            for line in reconcile_data["data"]
            if move_line.id in line.get("counterpart_line_ids", [])
        ]

    def _get_generated_other_lines(self, reconcile_data):
        return [
            line
            for line in reconcile_data["data"]
            if not line.get("id") and line.get("kind") == "other"
        ]

    def _create_taxed_invoice(self, partner, payment_term, move_type="out_invoice"):
        invoice = (
            self.env["account.move"]
            .with_context(default_move_type=move_type)
            .create(
                {
                    "move_type": move_type,
                    "partner_id": partner.id,
                    "invoice_date": "2019-01-01",
                    "date": "2019-01-01",
                    "invoice_payment_term_id": payment_term.id,
                    "invoice_line_ids": [
                        (
                            0,
                            0,
                            {
                                "name": "Taxed line",
                                "quantity": 1,
                                "price_unit": 100.0,
                                "tax_ids": [Command.set([self.tax_10.id])],
                            },
                        )
                    ],
                }
            )
        )
        invoice.action_post()
        return invoice

    @freeze_time("2019-01-10")
    def test_default_reconcile_data_uses_epd_if_eligible(self):
        partner = self.env["res.partner"].create({"name": "EPD Partner"})
        usd_currency = self.env["res.currency"].browse(self.currency_usd_id)

        epd_payment_term = self.env["account.payment.term"].create(
            {
                "name": "10% discount if paid within 10 days",
                "early_discount": True,
                "discount_percentage": 10.0,
                "discount_days": 10,
                "line_ids": [
                    Command.create(
                        {
                            "value": "percent",
                            "value_amount": 100,
                            "nb_days": 30,
                        }
                    )
                ],
            }
        )

        self.env["account.reconcile.model"].create(
            {
                "name": "EPD invoice matching",
                "sequence": 1,
                "rule_type": "invoice_matching",
                "match_partner": True,
                "match_partner_ids": [Command.set(partner.ids)],
                "match_text_location_label": True,
                "match_text_location_reference": False,
                "match_text_location_note": False,
                "allow_payment_tolerance": True,
                "payment_tolerance_type": "percentage",
                "payment_tolerance_param": 0.0,
                "match_same_currency": True,
                "past_months_limit": False,
                "company_id": self.company.id,
            }
        )

        invoice = self._create_invoice(
            move_type="out_invoice",
            invoice_amount=100.0,
            currency_id=self.currency_usd_id,
            partner_id=partner.id,
            payment_term_id=epd_payment_term.id,
            date_invoice="2019-01-01",
            auto_validate=True,
        )
        receivable_line = invoice.line_ids.filtered(
            lambda line: line.account_id.account_type
            in ("asset_receivable", "liability_payable")
        )
        self.assertEqual(len(receivable_line), 1)
        self.assertTrue(receivable_line.name)
        self.assertTrue(receivable_line.discount_date)
        self.assertEqual(
            receivable_line.discount_date.strftime("%Y-%m-%d"), "2019-01-11"
        )
        self.assertTrue(receivable_line.discount_amount_currency)

        company_amount = usd_currency._convert(
            90.0,
            self.company.currency_id,
            self.company,
            fields.Date.from_string("2019-01-10"),
        )

        bank_stmt = self.acc_bank_stmt_model.create(
            {
                "journal_id": self.bank_journal_euro.id,
                "date": "2019-01-10",
                "name": "epd-test",
            }
        )
        bank_stmt_line = self.acc_bank_stmt_line_model.create(
            {
                "name": "epd-line",
                "journal_id": self.bank_journal_euro.id,
                "statement_id": bank_stmt.id,
                "amount": company_amount,
                "foreign_currency_id": usd_currency.id,
                "amount_currency": 90.0,
                "date": "2019-01-10",
                "payment_ref": receivable_line.name,
                "partner_id": partner.id,
            }
        )

        reconcile_data = bank_stmt_line._default_reconcile_data()

        counterpart_lines = [
            line
            for line in reconcile_data["data"]
            if receivable_line.id in line.get("counterpart_line_ids", [])
        ]
        self.assertTrue(counterpart_lines)
        self.assertTrue(len(reconcile_data["data"]) == 3)
        self.assertTrue(
            reconcile_data["data"][2]["account_id"][1] == "443000 Cash Discount Loss"
        )

    @freeze_time("2019-01-10")
    def test_invoice_matching_with_discount(self):
        partner = self.env["res.partner"].create({"name": "EPD Partner Invoice"})
        payment_term = self._create_epd_payment_term(discount_percentage=10.0)
        self._create_invoice_matching_model(partner)
        invoice = self._create_taxed_invoice(
            partner, payment_term, move_type="out_invoice"
        )
        receivable_line = self._get_receivable_or_payable_line(invoice)
        bank_stmt_line = self._create_statement_line(
            amount=99.0,
            payment_ref=receivable_line.name,
            partner=partner,
        )

        reconcile_data = bank_stmt_line._default_reconcile_data()
        self.assertTrue(reconcile_data.get("can_reconcile"))
        self.assertTrue(self._get_counterpart_lines(reconcile_data, receivable_line))
        generated_other_lines = self._get_generated_other_lines(reconcile_data)
        self.assertTrue(generated_other_lines)
        self.assertTrue(
            any(line.get("tax_repartition_line_id") for line in generated_other_lines)
        )

        bank_stmt_line.reconcile_data_info = reconcile_data
        bank_stmt_line.reconcile_bank_line()
        self.assertTrue(bank_stmt_line.is_reconciled)
        self.assertEqual(invoice.amount_residual, 0.0)

    @freeze_time("2019-01-10")
    def test_vendor_bill_matching_with_discount(self):
        partner = self.env["res.partner"].create({"name": "EPD Partner Vendor"})
        payment_term = self._create_epd_payment_term(discount_percentage=10.0)
        self._create_invoice_matching_model(partner)
        vendor_bill = self._create_invoice(
            move_type="in_invoice",
            invoice_amount=100.0,
            currency_id=self.currency_euro_id,
            partner_id=partner.id,
            payment_term_id=payment_term.id,
            date_invoice="2019-01-01",
            auto_validate=True,
        )
        payable_line = self._get_receivable_or_payable_line(vendor_bill)
        bank_stmt_line = self._create_statement_line(
            amount=-90.0,
            payment_ref=payable_line.name,
            partner=partner,
        )

        reconcile_data = bank_stmt_line._default_reconcile_data()
        self.assertTrue(reconcile_data.get("can_reconcile"))
        self.assertTrue(self._get_counterpart_lines(reconcile_data, payable_line))

        bank_stmt_line.reconcile_data_info = reconcile_data
        bank_stmt_line.reconcile_bank_line()
        self.assertTrue(bank_stmt_line.is_reconciled)
        self.assertEqual(vendor_bill.amount_residual, 0.0)
        self.assertTrue(
            bank_stmt_line.move_id.line_ids.filtered(
                lambda line: line.account_id.account_type.startswith("income")
            )
        )

    @freeze_time("2019-01-10")
    def test_several_payment_terms_single_model(self):
        partner = self.env["res.partner"].create({"name": "EPD Partner Multi Terms"})
        self._create_invoice_matching_model(partner)
        payment_term_2 = self._create_epd_payment_term(discount_percentage=2.0)
        payment_term_3 = self._create_epd_payment_term(discount_percentage=3.0)

        for payment_term, payment_amount in (
            (payment_term_2, 98.0),
            (payment_term_3, 97.0),
        ):
            invoice = self._create_invoice(
                move_type="out_invoice",
                invoice_amount=100.0,
                currency_id=self.currency_euro_id,
                partner_id=partner.id,
                payment_term_id=payment_term.id,
                date_invoice="2019-01-01",
                auto_validate=True,
            )
            receivable_line = self._get_receivable_or_payable_line(invoice)
            bank_stmt_line = self._create_statement_line(
                amount=payment_amount,
                payment_ref=receivable_line.name,
                partner=partner,
            )
            reconcile_data = bank_stmt_line._default_reconcile_data()
            self.assertTrue(reconcile_data.get("can_reconcile"))
            self.assertTrue(
                self._get_counterpart_lines(reconcile_data, receivable_line)
            )
            self.assertTrue(self._get_generated_other_lines(reconcile_data))

    @freeze_time("2019-01-20")
    def test_no_discount_after_discount_date(self):
        partner = self.env["res.partner"].create({"name": "EPD Partner Late Payment"})
        payment_term = self._create_epd_payment_term(discount_percentage=10.0)
        self._create_invoice_matching_model(partner)
        invoice = self._create_invoice(
            move_type="out_invoice",
            invoice_amount=100.0,
            currency_id=self.currency_euro_id,
            partner_id=partner.id,
            payment_term_id=payment_term.id,
            date_invoice="2019-01-01",
            auto_validate=True,
        )
        receivable_line = self._get_receivable_or_payable_line(invoice)
        bank_stmt_line = self._create_statement_line(
            amount=90.0,
            payment_ref=receivable_line.name,
            partner=partner,
            date="2019-01-20",
        )

        reconcile_data = bank_stmt_line._default_reconcile_data()
        self.assertFalse(self._get_generated_other_lines(reconcile_data))
        self.assertTrue(self._get_counterpart_lines(reconcile_data, receivable_line))

        bank_stmt_line.reconcile_data_info = reconcile_data
        bank_stmt_line.reconcile_bank_line()
        self.assertEqual(invoice.amount_residual, 10.0)

    @freeze_time("2019-01-10")
    def test_full_payment_no_discount(self):
        partner = self.env["res.partner"].create({"name": "EPD Partner Full Payment"})
        payment_term = self._create_epd_payment_term(discount_percentage=10.0)
        self._create_invoice_matching_model(partner)
        invoice = self._create_invoice(
            move_type="out_invoice",
            invoice_amount=100.0,
            currency_id=self.currency_euro_id,
            partner_id=partner.id,
            payment_term_id=payment_term.id,
            date_invoice="2019-01-01",
            auto_validate=True,
        )
        receivable_line = self._get_receivable_or_payable_line(invoice)
        bank_stmt_line = self._create_statement_line(
            amount=100.0,
            payment_ref=receivable_line.name,
            partner=partner,
        )

        reconcile_data = bank_stmt_line._default_reconcile_data()
        self.assertTrue(reconcile_data.get("can_reconcile"))
        self.assertTrue(self._get_counterpart_lines(reconcile_data, receivable_line))
        self.assertFalse(self._get_generated_other_lines(reconcile_data))

    @freeze_time("2019-01-10")
    def test_auto_reconcile_with_discount(self):
        partner = self.env["res.partner"].create({"name": "EPD Partner Auto Edit"})
        payment_term = self._create_epd_payment_term(discount_percentage=10.0)
        self._create_invoice_matching_model(partner, auto_reconcile=True)
        invoice = self._create_invoice(
            move_type="out_invoice",
            invoice_amount=100.0,
            currency_id=self.currency_euro_id,
            partner_id=partner.id,
            payment_term_id=payment_term.id,
            date_invoice="2019-01-01",
            auto_validate=True,
        )
        receivable_line = self._get_receivable_or_payable_line(invoice)

        bank_stmt_line = self._create_statement_line(
            amount=90.0,
            payment_ref=receivable_line.name,
            partner=partner,
        )

        self.assertTrue(bank_stmt_line.is_reconciled)
        self.assertEqual(invoice.amount_residual, 0.0)
        self.assertFalse(
            bank_stmt_line.move_id.line_ids.filtered(
                lambda line: line.account_id
                == self.bank_journal_euro.suspense_account_id
            )
        )

    @freeze_time("2019-01-10")
    def test_auto_reconcile_keep_mode_with_discount(self):
        self.bank_journal_euro.reconcile_mode = "keep"
        self.bank_journal_euro.suspense_account_id.reconcile = True
        partner = self.env["res.partner"].create({"name": "EPD Partner Auto Keep"})
        payment_term = self._create_epd_payment_term(discount_percentage=10.0)
        self._create_invoice_matching_model(partner, auto_reconcile=True)
        invoice = self._create_invoice(
            move_type="out_invoice",
            invoice_amount=100.0,
            currency_id=self.currency_euro_id,
            partner_id=partner.id,
            payment_term_id=payment_term.id,
            date_invoice="2019-01-01",
            auto_validate=True,
        )
        receivable_line = self._get_receivable_or_payable_line(invoice)

        bank_stmt_line = self._create_statement_line(
            amount=90.0,
            payment_ref=receivable_line.name,
            partner=partner,
        )

        self.assertTrue(bank_stmt_line.is_reconciled)
        self.assertEqual(invoice.amount_residual, 0.0)
        self.assertIn(
            self.bank_journal_euro.suspense_account_id,
            bank_stmt_line.mapped("move_id.line_ids.account_id"),
        )

    @freeze_time("2019-01-10")
    def test_manual_add_with_discount(self):
        partner = self.env["res.partner"].create({"name": "EPD Partner Manual Add"})
        payment_term = self._create_epd_payment_term(discount_percentage=10.0)
        invoice = self._create_invoice(
            move_type="out_invoice",
            invoice_amount=100.0,
            currency_id=self.currency_euro_id,
            partner_id=partner.id,
            payment_term_id=payment_term.id,
            date_invoice="2019-01-01",
            auto_validate=True,
        )
        receivable_line = self._get_receivable_or_payable_line(invoice)
        bank_stmt_line = self._create_statement_line(
            amount=90.0,
            payment_ref="manual-add",
            partner=partner,
        )

        with Form(
            bank_stmt_line,
            view="account_reconcile_oca.bank_statement_line_form_reconcile_view",
        ) as form:
            form.add_account_move_line_id = receivable_line
            self.assertFalse(form.add_account_move_line_id)
            self.assertTrue(form.can_reconcile)

        self.assertTrue(
            self._get_generated_other_lines(bank_stmt_line.reconcile_data_info)
        )
        bank_stmt_line.reconcile_bank_line()
        self.assertTrue(bank_stmt_line.is_reconciled)
        self.assertEqual(invoice.amount_residual, 0.0)

    @freeze_time("2019-01-10")
    def test_manual_add_partial_without_discount(self):
        partner = self.env["res.partner"].create({"name": "EPD Partner Manual Partial"})
        payment_term = self._create_epd_payment_term(discount_percentage=10.0)
        invoice = self._create_invoice(
            move_type="out_invoice",
            invoice_amount=100.0,
            currency_id=self.currency_euro_id,
            partner_id=partner.id,
            payment_term_id=payment_term.id,
            date_invoice="2019-01-01",
            auto_validate=True,
        )
        receivable_line = self._get_receivable_or_payable_line(invoice)
        bank_stmt_line = self._create_statement_line(
            amount=70.0,
            payment_ref="manual-partial",
            partner=partner,
        )

        with Form(
            bank_stmt_line,
            view="account_reconcile_oca.bank_statement_line_form_reconcile_view",
        ) as form:
            form.add_account_move_line_id = receivable_line
            self.assertFalse(form.add_account_move_line_id)
            self.assertTrue(form.can_reconcile)

        self.assertFalse(
            any(
                line.get("tax_repartition_line_id")
                for line in self._get_generated_other_lines(
                    bank_stmt_line.reconcile_data_info
                )
            )
        )
        bank_stmt_line.reconcile_bank_line()
        self.assertTrue(bank_stmt_line.is_reconciled)
        self.assertEqual(invoice.amount_residual, 30.0)
