import unittest
import nesna_dev_pilot as m

class PilotTests(unittest.TestCase):
    def test_five_sources(self):
        self.assertEqual(len(m.config()),5)
        self.assertEqual({v['source'] for v in m.config().values()}, {'perm.Contract','perm.ContractField','perm.ContractDisbursement','perm.Ledger','perm.Note'})
    def test_field_parser(self):
        self.assertEqual(m.parse_expression('cast(RemovedFromSource as int) RemovedFromSource'),('int','RemovedFromSource','RemovedFromSource'))
        self.assertEqual(m.parse_expression('[ContractId]'),('column','ContractId','ContractId'))
        self.assertEqual(len(m.split_tsql_fields('ContractId,cast(RemovedFromSource as int) RemovedFromSource,Note')),3)
    def test_reject_unsupported(self):
        with self.assertRaises(ValueError): m.parse_expression('1; DROP TABLE public.permnote')
        with self.assertRaises(ValueError): m.raw_name_for('perm.Contract',['permContract','perm__Contract'])
        with self.assertRaises(ValueError): m.source_key('perm.Contract;DROP')
    def test_expected_names(self):
        self.assertEqual(m.raw_name_for('perm.Contract',['perm__Contract']), 'perm__Contract')
        self.assertEqual(m.raw_name_for('perm.Contract',['permContract']), 'permContract')

if __name__=='__main__': unittest.main()
