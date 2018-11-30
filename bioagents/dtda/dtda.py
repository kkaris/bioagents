# DTDA stands for disease-target-drug agent whose task is to
# search for targets known to be implicated in a
# certain disease and to look for drugs that are known
# to affect that target directly or indirectly

import re
import os
import numpy
import logging

from indra.sources.indra_db_rest import get_statements
from indra.databases import cbio_client
from bioagents import BioagentException

logger = logging.getLogger('DTDA')

_resource_dir = os.path.dirname(os.path.realpath(__file__)) + '/../resources/'


class DrugNotFoundException(BioagentException):
    pass


class DiseaseNotFoundException(BioagentException):
    pass


class DatabaseTimeoutError(BioagentException):
    pass


def _make_cbio_efo_map():
    lines = open(_resource_dir + 'cbio_efo_map.tsv', 'rt').readlines()
    cbio_efo_map = {}
    for lin in lines:
        cbio_id, efo_id = lin.strip().split('\t')
        try:
            cbio_efo_map[efo_id].append(cbio_id)
        except KeyError:
            cbio_efo_map[efo_id] = [cbio_id]
    return cbio_efo_map


cbio_efo_map = _make_cbio_efo_map()


class DTDA(object):
    def __init__(self):
        # Initialize cache of substitution statements, which will populate
        # on-the-fly from the database.
        self.sub_statements = {}

        # These two dicts will cache results from the database, and act as
        # a record of which targets and drugs have been search, which is why
        # the dicts are kept separate. That way we know that if Selumetinib
        # shows up in the drug_targets keys, all the targets of Selumetinib
        # will be present, while although Selumetinib may be a value in
        # target_drugs drugs, not all targets that have Selumetinib as a drug
        # will be keys.
        self.target_drugs = {}
        self.drug_targets = {}
        return

    def is_nominal_drug_target(self, drug, target):
        """Return True if the drug targets the target, and False if not."""
        targets = self.find_drug_targets(drug)
        if not targets:
            raise DrugNotFoundException
        if target.name in targets:
            return True
        return False

    def _get_tas_stmts(self, drug_term=None, target_term=None):
        timeout = 10
        drug = _convert_term(drug_term)
        target = _convert_term(target_term)
        resp = get_statements(subject=drug, object=target,
                              stmt_type='Inhibition', timeout=timeout,
                              simple_response=False)
        if resp.is_working():
            msg = ("Database has failed to respond after %d seconds looking "
                   "up %s inhibits %s." % (timeout, drug, target))
            logger.error(msg)
            raise DatabaseTimeoutError(msg)
        return (s for s in resp.statements
                if any(ev.source_api == 'tas' for ev in s.evidence))

    def _extract_terms(self, agent):
        term_set = {(ref, ns) for ns, ref in agent.db_refs.items()}

        # Try without a hyphen.
        if '-' in agent.name:
            term_set.add((agent.name.replace('-', ''), 'TEXT'))

        # Try different capitalizations.
        transforms = ['capitalize', 'upper', 'lower']
        for opp in map(lambda nm: getattr(agent.name, nm), transforms):
            term_set.add((opp(), 'TEXT'))

        return term_set

    def find_target_drugs(self, target):
        """Return all the drugs that target a given target."""
        # These are proteins/genes so we just look at HGNC grounding
        if 'HGNC' not in target.db_refs:
            return {}
        target_term = (target.db_refs['HGNC'], 'HGNC')
        # Check if we already have the stashed result
        if target_term not in self.target_drugs:
            try:
                drugs = {(s.subj.name, s.subj.db_refs.get('PUBCHEM'))
                         for s in self._get_tas_stmts(target_term=target_term)}
                self.target_drugs[target_term] = drugs
            except DatabaseTimeoutError:
                # TODO: We should return a special message if the database
                # can't be reached for some reason. It might also be good to
                # stash the cache dicts as back-ups.
                # If there is an error we don't stash the results
                return {}
        else:
            drugs = self.target_drugs[target_term]
        return drugs

    def find_drug_targets(self, drug):
        """Return all the targets of a given drug."""
        # Build a list of different possible identifiers
        drug_terms = self._extract_terms(drug)

        # Search for relations involving those identifiers.
        all_targets = set()
        for term in drug_terms:
            if term not in self.drug_targets:
                try:
                    tas_stmts = self._get_tas_stmts(term)
                except DatabaseTimeoutError:
                    continue
                targets = {s.obj.name for s in tas_stmts}
                self.drug_targets[term] = targets
            else:
                targets = self.drug_targets[term]
            all_targets |= targets
        return all_targets

    def find_mutation_effect(self, protein_name, amino_acid_change):
        match = re.match(r'([A-Z])([0-9]+)([A-Z])', amino_acid_change)
        if match is None:
            return None
        matches = match.groups()
        wt_residue = matches[0]
        pos = matches[1]
        sub_residue = matches[2]

        if protein_name not in self.sub_statements:
            logger.info("Looking up: %s" % protein_name)
            self.sub_statements[protein_name] \
                = get_statements(agents=[protein_name], stmt_type='ActiveForm')
        else:
            logger.info("Already have info for: %s." % protein_name)

        for stmt in self.sub_statements[protein_name]:
            mutations = stmt.agent.mutations
            # Make sure the Agent has exactly one mutation
            if len(mutations) != 1:
                continue
            if mutations[0].residue_from == wt_residue and\
                mutations[0].position == pos and\
                mutations[0].residue_to == sub_residue:
                    if stmt.is_active:
                        return 'activate'
                    else:
                        return 'deactivate'
        return None

    @staticmethod
    def _get_studies_from_disease_name(disease_name):
        study_prefixes = cbio_efo_map.get(disease_name)
        if study_prefixes is None:
            return None
        study_ids = []
        for sp in study_prefixes:
            study_ids += cbio_client.get_cancer_studies(sp)
        return list(set(study_ids))

    def get_mutation_statistics(self, disease_name, mutation_type):
        study_ids = self._get_studies_from_disease_name(disease_name)
        if not study_ids:
            raise DiseaseNotFoundException
        gene_list = self._get_gene_list()
        mutation_dict = {}
        num_case = 0
        logger.info("Found %d studies and a gene_list of %d elements."
                    % (len(study_ids), len(gene_list)))
        for study_id in study_ids:
            num_case += cbio_client.get_num_sequenced(study_id)
            mutations = cbio_client.get_mutations(study_id, gene_list,
                                                  mutation_type)
            if not mutations['gene_symbol']:
                logger.info("Found no genes for %s." % study_id)
                continue

            # Get the most mutated gene.
            top_gene = max(mutations['gene_symbol'],
                           key=lambda g: mutations['gene_symbol'].count(g))
            logger.info("Found %d genes, with top hit %s for %s."
                        % (len(set(mutations['gene_symbol'])), top_gene,
                           study_id))

            # Get the mutations effects for that gene.
            for g, a in zip(mutations['gene_symbol'],
                            mutations['amino_acid_change']):
                if g != top_gene:
                    continue

                mutation_effect = self.find_mutation_effect(g, a)
                if mutation_effect is None:
                    mutation_effect_key = 'other'
                else:
                    mutation_effect_key = mutation_effect
                try:
                    mutation_dict[g][0] += 1.0
                    mutation_dict[g][1][mutation_effect_key] += 1
                except KeyError:
                    effect_dict = {'activate': 0.0, 'deactivate': 0.0,
                                   'other': 0.0}
                    effect_dict[mutation_effect_key] += 1.0
                    mutation_dict[g] = [1.0, effect_dict]
        # Normalize entries
        for k, v in mutation_dict.items():
            mutation_dict[k][0] /= num_case
            effect_sum = numpy.sum(list(v[1].values()))
            mutation_dict[k][1]['activate'] /= effect_sum
            mutation_dict[k][1]['deactivate'] /= effect_sum
            mutation_dict[k][1]['other'] /= effect_sum

        return mutation_dict

    def get_top_mutation(self, disease_name):
        # First, look for possible disease targets
        try:
            mutation_stats = self.get_mutation_statistics(disease_name,
                                                          'missense')
        except DiseaseNotFoundException as e:
            logger.exception(e)
            raise DiseaseNotFoundException
        if mutation_stats is None:
            logger.error('No mutation stats')
            return None

        # Return the top mutation as a possible target
        mutations_sorted = sorted(mutation_stats.items(),
                                  key=lambda x: x[1][0],
                                  reverse=True)
        top_mutation = mutations_sorted[0]
        mut_protein = top_mutation[0]
        mut_percent = int(top_mutation[1][0]*100.0)
        # TODO: return mutated residues
        # mut_residues =
        return mut_protein, mut_percent

    def _get_gene_list(self):
        gene_list = []
        for one_list in self.gene_lists.values():
            gene_list += one_list
        return gene_list

    gene_lists = {
        'rtk_signaling':
        ["EGFR", "ERBB2", "ERBB3", "ERBB4", "PDGFA", "PDGFB",
         "PDGFRA", "PDGFRB", "KIT", "FGF1", "FGFR1", "IGF1",
         "IGF1R", "VEGFA", "VEGFB", "KDR"],
        'pi3k_signaling':
        ["PIK3CA", "PIK3R1", "PIK3R2", "PTEN", "PDPK1", "AKT1",
         "AKT2", "FOXO1", "FOXO3", "MTOR", "RICTOR", "TSC1", "TSC2",
         "RHEB", "AKT1S1", "RPTOR", "MLST8"],
        'mapk_signaling':
        ["KRAS", "HRAS", "BRAF", "RAF1", "MAP3K1", "MAP3K2", "MAP3K3",
         "MAP3K4", "MAP3K5", "MAP2K1", "MAP2K2", "MAP2K3", "MAP2K4",
         "MAP2K5", "MAPK1", "MAPK3", "MAPK4", "MAPK6", "MAPK7", "MAPK8",
         "MAPK9", "MAPK12", "MAPK14", "DAB2", "RASSF1", "RAB25"]
        }


class Disease(object):
    def __init__(self, disease_type, name, db_refs):
        self.disease_type = disease_type
        self.name = name
        self.db_refs = db_refs

    def __repr__(self):
        return 'Disease(%s, %s, %s)' % \
            (self.disease_type, self.name, self.db_refs)

    def __str__(self):
        return self.__repr__()


def _convert_term(term):
    if term is not None:
        return '%s@%s' % tuple(term)
    return
