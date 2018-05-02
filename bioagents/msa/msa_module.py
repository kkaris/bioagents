import os
import sys
import re
import pickle
import logging


from indra.assemblers import SBGNAssembler

logging.basicConfig(format='%(levelname)s: %(name)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger('MSA')

from kqml import KQMLPerformative, KQMLList

from indra.sources.trips.processor import TripsProcessor
from indra import has_config

from bioagents import Bioagent


if has_config('INDRA_DB_REST_URL') and has_config('INDRA_DB_REST_API_KEY'):
    from indra.sources.indra_db_rest import get_statements, IndraDBRestError

    CAN_CHECK_STATEMENTS = True
else:
    logger.warning("Database web api not specified. Cannot get background.")
    CAN_CHECK_STATEMENTS = False


def _read_signor_afs():
    path = os.path.dirname(os.path.abspath(__file__)) + \
            '/../resources/signor_active_forms.pkl'
    with open(path, 'rb') as pkl_file:
        stmts = pickle.load(pkl_file)
    if isinstance(stmts, dict):
        signor_afs = []
        for _, stmt_list in stmts.items():
            signor_afs += stmt_list
    else:
        signor_afs = stmts
    return signor_afs


class MSA_Module(Bioagent):
    name = 'MSA'
    tasks = ['PHOSPHORYLATION-ACTIVATING', 'FIND-IMMEDIATE-RELATION']
    signor_afs = _read_signor_afs()

    def respond_phosphorylation_activating(self, content):
        """Return response content to phosphorylation_activating request."""
        if not CAN_CHECK_STATEMENTS:
            return self.make_failure(
                'NO_KNOWLEDGE_ACCESS',
                'Cannot access the database through the web api.'
                )
        heading = content.head()
        m = re.match('(\w+)-(\w+)', heading)
        if m is None:
            return self.make_failure('UNKNOWN_ACTION')
        action, polarity = [s.lower() for s in m.groups()]
        target_ekb = content.gets('target')
        if target_ekb is None or target_ekb == '':
            return self.make_failure('MISSING_TARGET')
        agent = self._get_agent(target_ekb)
        logger.debug('Found agent (target): %s.' % agent.name)
        residue = content.gets('residue')
        position = content.gets('position')
        related_result_dict = {}
        logger.info("Looking for statements with agent %s of type %s."
                    % (str(agent), 'ActiveForm'))
        for namespace, name in agent.db_refs.items():
            # TODO: Remove this eventually, as it is a temporary work-around.
            if namespace == 'FPLX':
                namespace = 'BE'
            logger.info("Checking namespace: %s" % namespace)
            stmts = get_statements(agents=['%s@%s' % (name, namespace)],
                                   stmt_type='ActiveForm')
            for s in stmts:
                if self._matching(s, residue, position, action, polarity):
                    related_result_dict[s.matches_key()] = s
        logger.info("Found %d matching statements." % len(related_result_dict))
        if not len(related_result_dict):
            return self.make_failure(
                'MISSING_MECHANISM',
                "Could not find statement matching phosphorylation activating "
                "%s, %s, %s, %s." % (agent.name, residue, position,
                                     'phosphorylation')
                )
        else:
            self.send_provenance_for_stmts(
                related_result_dict.values(),
                "Phosphorylation at %s%s activates %s." % (
                    residue,
                    position,
                    agent.name
                    )
                )
            msg = KQMLPerformative('SUCCESS')
            msg.set('is-activating', 'TRUE')
            return msg

    def respond_find_immediate_relation(self, content):
        """Find statements matching a query for FIND-IMMEDIATE-RELATION task."""
        agent_dict = dict.fromkeys(['subject', 'object'])
        for pos, loc in [('subject', 'source'), ('object', 'target')]:
            ekb = content.gets(loc)
            try:
                agent = self._get_agent(ekb)
                if agent is None or agent == 'None':
                    agent_dict[pos] = None
                else:
                    agent_dict[pos] = {'name': agent.name}
                    agent_dict[pos].update(agent.db_refs)
            except Exception as e:
                logger.error("Got exception while converting ekb for %s "
                             "(%s) into an agent." % (pos, ekb))
                logger.exception(e)
                return self.make_failure('MISSING_TARGET')
        stmt_type = content.gets('type')
        if stmt_type == 'unknown':
            stmt_type = None
        nl_question = ('{subject} {verb} of {object}'
                       .format(verb=stmt_type,
                               **{k: None if v is None else v['name']
                                  for k, v in agent_dict.items()}))
        logger.info("Got a query for %s." % nl_question)
        # Try to get related statements.
        try:
            input_dict = {'stmt_type': stmt_type}

            # Use the best available db ref for each agent.
            for pos, ref_dict in agent_dict.items():
                if ref_dict is None:
                    input_dict[pos] = None
                else:
                    for key in ['HGNC', 'FPLX', 'CHEBI', 'name', 'TEXT']:
                        if key in ref_dict.keys():
                            input_dict[pos] = ref_dict[key]

            # Actually get the statements.
            stmts = get_statements(**input_dict)
        except IndraDBRestError as e:
            logger.error("Failed to get statements.")
            logger.exception(e)
            return self.make_failure('MISSING_MECHANISM')

        # For now just list the statements in the provenance tab. Only captures
        # the top 5.
        try:
            self.send_display_stmts(stmts, nl_question)
        except Exception as e:
            logger.warning("Failed to send provenance.")
            logger.exception(e)

        # Assuming we haven't hit any errors yet, return SUCCESS
        resp = KQMLPerformative('SUCCESS')
        resp.set('relations-found', str(len(stmts)))
        return resp

    def send_display_stmts(self, stmts, nl_question):
        self.send_table_to_provenance(stmts, nl_question)
        logger.info('Sending display statements')
        resource = _make_sbgn(stmts[10:])
        logger.info(resource)
        content = KQMLList('open-query-window')
        content.sets('cyld', '#1')
        content.sets('graph', resource)
        self.tell(content)

    def send_table_to_provenance(self, stmts, nl_question):
        """Post a concise table listing statements found."""
        html_str = '<h4>Statements matching: %s</h4>\n' % nl_question
        html_str += '<table style="width:100%">\n'
        row_list = ['<th>Source</th><th>Target</th><th>Interaction</th>']
        for stmt in stmts:
            sub_ag, obj_ag = stmt.agent_list()
            row_list.append('<td>{subject}</td><td>{verb}</td><td>{object}</td>'
                            .format(subject=sub_ag.name, object=obj_ag.name,
                                    verb=type(stmt)))
        html_str += '\n'.join(['  <tr>%s</tr>\n' % row_str
                               for row_str in row_list])
        html_str += '</table>'
        content = KQMLList('add-provenance')
        content.sets('html', html_str)
        return self.tell(content)

    @staticmethod
    def _get_agent(agent_ekb):
        tp = TripsProcessor(agent_ekb)
        terms = tp.tree.findall('TERM')
        if len(terms):
            term_id = terms[0].attrib['id']
            agent = tp._get_agent_by_id(term_id, None)
        else:
            agent = None
        return agent

    def _matching(self, stmt, residue, position, action, polarity):
        if stmt.is_active is not (polarity == 'activating'):
            return False
        matching_residues = any([
            m.residue == residue
            and m.position == position
            and m.mod_type == action
            for m in stmt.agent.mods])
        return matching_residues


def _make_sbgn(stmts):
    sa = SBGNAssembler()
    sa.add_statements(stmts)
    sa.make_model()
    sbgn_str = sa.print_model()
    logger.info(sbgn_str)
    return sbgn_str


if __name__ == "__main__":
    MSA_Module(argv=sys.argv[1:])
