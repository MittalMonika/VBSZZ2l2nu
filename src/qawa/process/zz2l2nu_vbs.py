import awkward as ak
import numpy as np
import scipy.interpolate as interp
from scipy import stats as st
import uproot
import pickle
import hist
import yaml
import os
import re

from coffea import processor
from coffea.nanoevents.methods import candidate

from coffea.analysis_tools import Weights, PackedSelection
from coffea.lumi_tools import LumiMask

from qawa.roccor import rochester_correction
from qawa.applyGNN import applyGNN
from qawa.leptonsSF import LeptonScaleFactors
from qawa.btag import BTVCorrector, btag_id
from qawa.jme import JMEUncertainty, update_collection
from qawa.common import pileup_weights, ewk_corrector, met_phi_xy_correction, theory_ps_weight, theory_pdf_weight, trigger_rules,getPhotonTrigPrescale, PhotonSF

def build_leptons(muons, electrons):
    # select tight/loose muons
    tight_muons_mask = (
        (muons.pt             >  20. ) &
        (np.abs(muons.eta)    <  2.4 ) &
        (np.abs(muons.dxy)    <  0.02) &
        (np.abs(muons.dz )    <  0.1 ) &
        (muons.pfRelIso04_all <= 0.15) & 
        muons.tightId
    )
    tight_muons = muons[tight_muons_mask]
    loose_muons = muons[
        ~tight_muons_mask &
        (muons.pt            >  10. ) &
        (np.abs(muons.eta)   <  2.4 ) &
        (muons.pfRelIso04_all<= 0.25) &
        muons.softId   
    ]
    # select tight/loose electron
    tight_electrons_mask = (
        (electrons.pt           > 20.) &
        (np.abs(electrons.eta)  < 2.5) &
        electrons.mvaFall17V2Iso_WP90
    )
    tight_electrons = electrons[tight_electrons_mask]
    loose_electrons = electrons[
        ~tight_electrons_mask &
        (electrons.pt           > 10. ) &
        (np.abs(electrons.eta)  < 2.5) &
        electrons.mvaFall17V2Iso_WPL
    ]
    
    extra_mu_inph = muons[
        (muons.pt            >  5. ) &
        (np.abs(muons.eta)   <  2.4 ) &
        (muons.pfRelIso03_all <= 0.4) &
        (muons.mediumId)
    ]
    extra_el_inph = electrons[
        (electrons.pt           > 5. ) &
        (np.abs(electrons.eta)  < 2.5) &
        (electrons.mvaFall17V2noIso_WP90 ) &
        (electrons.pfRelIso03_all <=0.4)
    ] 

    # contruct a lepton object
    tight_leptons = ak.with_name(ak.concatenate([tight_muons, tight_electrons], axis=1), 'PtEtaPhiMCandidate')
    loose_leptons = ak.with_name(ak.concatenate([loose_muons, loose_electrons], axis=1), 'PtEtaPhiMCandidate')
    extra_leptons_inph = ak.with_name(ak.concatenate([extra_mu_inph,extra_el_inph], axis=1), 'PtEtaPhiMCandidate')
    return tight_leptons, loose_leptons,extra_leptons_inph

def build_htaus(tau, lepton):
    base = (
        (tau.pt         > 18. ) & 
        (np.abs(tau.eta)< 2.3 ) & 
        (tau.decayMode != 5   ) & 
        (tau.decayMode != 6   )
    )
    overlap_leptons = ak.any(
        tau.metric_table(lepton) <= 0.4,
        axis=2
    )
    return tau[base & ~overlap_leptons]

def build_photons(photon):
    base = (
        (photon.pt          > 20. ) & 
        (np.abs(photon.eta) < 2.5 )
    )

    dd_photons_base = (
        (photon.pt          > 55. ) &
        (np.abs(photon.eta) <= 2.5 ) &
        (~(photon.pixelSeed)) &
        (photon.electronVeto) &
        (photon.sieie >= 0.001) &
        (photon.cutBased ==3)
    )
    dd_photons_loose = photon[
        (~dd_photons_base) &
        (base) &
        (photon.cutBased ==3)
    ]
    
    # MVA ID
    tight_photons = photon[base & photon.mvaID_WP90]
    loose_photons = photon[base & photon.mvaID_WP80 & ~photon.mvaID_WP90]

    dd_photons = photon[dd_photons_base]
    
    # cut based ID
    return tight_photons, loose_photons, dd_photons_loose, dd_photons



class zzinc_processor(processor.ProcessorABC):
    # EWK corrections process has to be define before hand, it has to change when we move to dask
    def __init__(self, era: str ='2018', dump_gnn_array=False, ewk_process_name=None): 
        self._era = era
        if 'APV' in self._era:
            self._isAPV = True
            self._era = re.findall(r'\d+', self._era)[0] 
            print(f"[YACINE DEBUG] era={self._era} APV={self._isAPV}")
        else:
            self._isAPV = False
        
        jec_tag = ''
        jer_tag = ''
        if self._era == '2016':
            if self._isAPV:
                jec_tag = 'Summer19UL16APV_V7_MC'
                jer_tag = 'Summer20UL16APV_JRV3_MC'
            else:
                jec_tag = 'Summer19UL16_V7_MC'
                jer_tag = 'Summer20UL16_JRV3_MC'
        elif self._era == '2017':
            jec_tag = 'Summer19UL17_V5_MC'
            jer_tag = 'Summer19UL17_JRV2_MC'
        elif self._era == '2018':
            jec_tag = 'Summer19UL18_V5_MC'
            jer_tag = 'Summer19UL18_JRV2_MC'
        else:
            print('error')
        
        self.btag_wp = 'L'
        self.zmass = 91.1873 # GeV
        self._btag = BTVCorrector(era=self._era, wp=self.btag_wp, isAPV=self._isAPV)
        self._jmeu = JMEUncertainty(jec_tag, jer_tag)
        self._purw = pileup_weights(era=self._era)
        self._leSF = LeptonScaleFactors(era=self._era, isAPV=self._isAPV)

        _data_path = 'qawa/data'
        _data_path = os.path.join(os.path.dirname(__file__), '../data')
        self._phEval = PhotonSF(_data_path,era=self._era)
        #if its is photon CR 
        self._isnotpho= False

        
        self._json = {
            '2018': LumiMask(f'{_data_path}/json/Cert_314472-325175_13TeV_Legacy2018_Collisions18_JSON.txt'),
            '2017': LumiMask(f'{_data_path}/json/Cert_294927-306462_13TeV_UL2017_Collisions17_GoldenJSON.txt'),
            '2016': LumiMask(f'{_data_path}/json/Cert_271036-284044_13TeV_Legacy2016_Collisions16_JSON.txt'),
        }
        with open(f'{_data_path}/{self._era}-trigger-rules.yaml') as ftrig:
            self._triggers = yaml.load(ftrig, Loader=yaml.FullLoader)
        
        with open(f'{_data_path}/eft-names.dat') as eft_file:
            self._eftnames = [n.strip() for n in eft_file.readlines()]

        #adding prescale value to triggers
        with open(f'{_data_path}/Photon/{self._era}-photon.yaml') as ftrigpre:
            self._triggers_prescale = yaml.load(ftrigpre, Loader=yaml.FullLoader)
        with uproot.open(f'{_data_path}/trigger_sf/histo_triggerEff_sel0_{self._era}.root') as _fn:
            _hvalue = np.dstack([_fn[_hn].values() for _hn in _fn.keys()] + [np.ones((7,7))])
            _herror = np.dstack([np.sqrt(_fn[_hn].variances()) for _hn in _fn.keys()] + [np.zeros((7,7))])
            self.trig_sf_map = np.stack([_hvalue, _herror], axis=-1)
        self.dump_gnn_array  = dump_gnn_array
        with open(f'{_data_path}/GNNmodel/gnn_flattening_fnc_{era}.pkl', 'rb') as _fn:
            self.gnn_flat_fnc = pickle.load(_fn)

        self.ewk_process_name = ewk_process_name

    
        if self.ewk_process_name is not None :
            if self.ewk_process_name != "GJets":
                self.ewk_corr = ewk_corrector(process=ewk_process_name)
        self.build_histos = lambda: {
            'dilep_mt': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(60, 0, 600, name="dilep_mt", label=r"$M_{T}^{\ell\ell}$ (GeV)"),
                hist.storage.Weight()
            ), 
            'dilep_pt': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(60, 0, 600, name="dilep_pt", label=r"$p_{T}^{\ell\ell}$ (GeV)"),
                hist.storage.Weight()
            ), 
            'dilep_eta': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, -5, 5, name="dilep_eta", label=r"$\eta_{\ell\ell}$ (GeV)"),
                hist.storage.Weight()
            ), 
            'dilep_m': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(60, 0, 120, name="dilep_m", label=r"$M_{\ell\ell}$ (GeV)"),
                hist.storage.Weight()
            ), 
            'met_pt': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(60, 0, 600, name="met_pt", label=r"$p_{T}^{miss}$ (GeV)"),
                hist.storage.Weight()
            ),
            'njets': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(5, 0, 5, name="njets", label=r"$N_{jet}$ ($p_{T}>30$ GeV)"),
                hist.storage.Weight()
            ), 
            'bjets': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(5, 0, 5, name="bjets", label=r"$N_{b-jet}$ ($p_{T}>30$ GeV)"),
                hist.storage.Weight()
            ),
            'dphi_met_ll': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 0, np.pi, name="dphi_met_ll", label=r"$\Delta \phi(\ell\ell,p_{T}^{miss})$"),
                hist.storage.Weight()
            ),
            'gnn_score': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 0, 1, name="gnn_score", label=r"$O_{GNN}$"),
                hist.storage.Weight()
            ),
            'gnn_flat': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 0, 1, name="gnn_flat", label=r"$O_{GNN}$"),
                hist.storage.Weight()
            ),
            'dijet_mass': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 0, 2000, name="dijet_mass", label=r"$m_{jj}$ (GeV)"),
                hist.storage.Weight() 
            ),
            'dijet_deta': hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True),
                hist.axis.Regular(20, 0, 8, name="dijet_deta", label=r"$Delta\eta_{jj}$"),
                hist.storage.Weight()
            ),
            "lead_jet_pt": hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 30, 530, name="lead_jet_pt", label="$p_T^{j_1}$ (GeV)"),
                hist.storage.Weight()
            ), 
            "trail_jet_pt": hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 30, 530, name="trail_jet_pt", label=r"$p_T^{j_2}$ (GeV)"),
                hist.storage.Weight()
            ),
            "lead_jet_eta": hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, -5, 5, name="lead_jet_eta", label=r"$\eta(j_1)$"),
                hist.storage.Weight()
            ), 
            "trail_jet_eta": hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, -5, 5, name="trail_jet_eta", label=r"$\eta(j_2)$"),
                hist.storage.Weight()
            ),
            "min_dphi_met_j": hist.Hist( 
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 0, np.pi, name="min_dphi_met_j", label=r"$\min\Delta\phi(p_{T}^{miss},j)$"),
                hist.storage.Weight()
            ),
            "delta_phi_Zjets_met":hist.Hist(
                hist.axis.StrCategory([], name="channel"   , growth=True),
                hist.axis.StrCategory([], name="systematic", growth=True), 
                hist.axis.Regular(50, 0, np.pi, name="delta_phi_Zjets_met", label=r"$\min\Delta\phi(p_{T}^{miss},Z+j)$"),
                hist.storage.Weight()
            ),
        }

    
    def _add_trigger_sf(self, weights, lead_lep, subl_lep):
        mask_BB = ak.fill_none((lead_lep.eta <= 1.5) & (subl_lep.eta <= 1.5), False)
        mask_EB = ak.fill_none((lead_lep.eta >= 1.5) & (subl_lep.eta <= 1.5), False)
        mask_BE = ak.fill_none((lead_lep.eta <= 1.5) & (subl_lep.eta >= 1.5), False)
        mask_EE = ak.fill_none((lead_lep.eta >= 1.5) & (subl_lep.eta >= 1.5), False)

        mask_mm = ak.fill_none((np.abs(lead_lep.pdgId)==13) & (np.abs(subl_lep.pdgId)==13), False)
        mask_ee = ak.fill_none((np.abs(lead_lep.pdgId)==11) & (np.abs(subl_lep.pdgId)==11), False)
       
        mask_me = (~mask_mm & ~mask_ee) & (np.abs(lead_lep.pdgId) == 13)
        mask_em = (~mask_mm & ~mask_ee) & (np.abs(lead_lep.pdgId) == 11)

        lept_pt_bins = [20, 25, 30, 35, 40, 50, 60, 100000]
        lep_1_bin = np.digitize(lead_lep.pt.to_numpy(), lept_pt_bins) - 1
        lep_2_bin = np.digitize(subl_lep.pt.to_numpy(), lept_pt_bins) - 1
        trigg_bin = np.select([
            (mask_ee & mask_BB).to_numpy(),
            (mask_ee & mask_BE).to_numpy(),
            (mask_ee & mask_EB).to_numpy(),
            (mask_ee & mask_EE).to_numpy(),

            (mask_em & mask_BB).to_numpy(),
            (mask_em & mask_BE).to_numpy(),
            (mask_em & mask_EB).to_numpy(),
            (mask_em & mask_EE).to_numpy(),

            (mask_me & mask_BB).to_numpy(),
            (mask_me & mask_BE).to_numpy(),
            (mask_me & mask_EB).to_numpy(),
            (mask_me & mask_EE).to_numpy(),

            (mask_mm & mask_BB).to_numpy(),
            (mask_mm & mask_BE).to_numpy(),
            (mask_mm & mask_EB).to_numpy(),
            (mask_mm & mask_EE).to_numpy()
        ], np.arange(0,16), 16)

        # this is to avoid cases were two 
        # leptons are not in the event
        lep_1_bin[lep_1_bin>6] = -1
        lep_2_bin[lep_2_bin>6] = -1
        center_value = self.trig_sf_map[lep_1_bin,lep_2_bin,trigg_bin,0]
        errors_value = self.trig_sf_map[lep_1_bin,lep_2_bin,trigg_bin,1]
        
        #weights.add(
        #    'triggerSF', 
        #    center_value, 
        #    center_value + errors_value,
        #    center_value - errors_value
        #)


    def process_shift(self, event, shift_name:str=''):
        dataset = event.metadata['dataset']
        is_data = event.metadata.get("is_data")
        selection = PackedSelection()
        weights = Weights(len(event), storeIndividual=True)
        
        histos = self.build_histos()
        
        if is_data:
            goodlumi_ = ak.Array(self._json[self._era](event.run, event.luminosityBlock))
            selection.add('lumimask', self._json[self._era](event.run, event.luminosityBlock))
            selection.add('triggers', trigger_rules(event, self._triggers, self._era))
        else:
            selection.add('lumimask', np.ones(len(event), dtype='bool'))
            selection.add('triggers', np.ones(len(event), dtype='bool'))
        
        # MET filters
        if '2016' in self._era :
            selection.add(
                'metfilter',
                #event.Flag.METFilters &
                event.Flag.goodVertices &
                event.Flag.globalSuperTightHalo2016Filter &
                event.Flag.HBHENoiseFilter &
                event.Flag.HBHENoiseIsoFilter & 
                event.Flag.EcalDeadCellTriggerPrimitiveFilter & 
                event.Flag.BadPFMuonFilter &
                event.Flag.BadPFMuonDzFilter &
                event.Flag.eeBadScFilter )

        else :
            if is_data :
                selection.add(
                    'metfilter',
                    #event.Flag.METFilters &
                    event.Flag.goodVertices &
                    event.Flag.globalSuperTightHalo2016Filter &
                    event.Flag.HBHENoiseFilter &
                    event.Flag.HBHENoiseIsoFilter & 
                    event.Flag.EcalDeadCellTriggerPrimitiveFilter & 
                    event.Flag.BadPFMuonFilter &
                    event.Flag.BadPFMuonDzFilter &
                    event.Flag.eeBadScFilter &
                    event.Flag.ecalBadCalibFilter ) 
            else :    
                 selection.add(
                     'metfilter',
                     #event.Flag.METFilters &
                     event.Flag.goodVertices &
                     event.Flag.globalSuperTightHalo2016Filter &
                     event.Flag.HBHENoiseFilter &
                     event.Flag.HBHENoiseIsoFilter & 
                     event.Flag.EcalDeadCellTriggerPrimitiveFilter & 
                     event.Flag.BadPFMuonFilter &
                     event.Flag.BadPFMuonDzFilter &
                     event.Flag.eeBadScFilter &
                     event.Flag.ecalBadCalibFilter ) 
        #photon CR
        tight_photons, loose_photons, dd_photons_loose, dd_photons = build_photons(event.Photon)
        #tight_photons,loose_photons = build_photons(event.Photon)
        lead_photon=ak.firsts(dd_photons)
        
        ntight_pho = ak.num(dd_photons)
        nloose_pho = ak.num(dd_photons_loose)


        _data_path = 'qawa/data'
        _data_path = os.path.join(os.path.dirname(__file__), '../data')
        
        if not self._isnotpho:
            if  is_data:
                event.prescaleweight = getPhotonTrigPrescale(_data_path,event.run, event.luminosityBlock, self._triggers_prescale, lead_photon.pt, goodlumi_,self._era)

            else :    
                self._phSF = self._phEval["EGamma_SF2D_T"](lead_photon.eta,lead_photon.pt)
                self._phSFerr = self._phSF + self._phEval["EGamma_SF2D_T_err"](lead_photon.eta,lead_photon.pt)
                gjetsnnlo = 1.716910- (np.multiply(lead_photon.pt, 0.001221))
        #IDscale factor
        #need to get SF from https://github.com/jdulemba/NanoAOD_Analyses/blob/new_kfactor_files/Analysis/python/LeptonSF.py

        tight_lep, loose_lep,extra_lep_inph = build_leptons(
            event.Muon,
            event.Electron
        )
        
        nextra_lep_inph = ak.num(extra_lep_inph)
        had_taus = build_htaus(event.Tau, tight_lep)
        had_taus_ph = build_htaus(event.Tau, dd_photons)
        ntight_lep = ak.num(tight_lep)
        nloose_lep = ak.num(loose_lep)
        nhtaus_lep = ak.num(had_taus)
        nhtaus_ph = ak.num(had_taus_ph)
        
        jets = event.Jet
        if (self._isnotpho):
            overlap_leptons =  ak.any(
                jets.metric_table(tight_lep) <= 0.4,
                axis=2
            )    
        else:
            overlap_leptons = ak.any(
                jets.metric_table(dd_photons) <= 0.4,
                axis=2
            )
            
        pileup_jets =  ( (jets.pt>30.0) & (jets.pt<50.0) & (jets.puId ==7) ) | ((jets.pt>50.0))
        
        jet_mask = (
            (~overlap_leptons) & 
            (pileup_jets) &
            (jets.pt>30.0) & 
            (np.abs(jets.eta) < 4.7) & 
            (jets.jetId >= 6) # tight JetID 7(2016) and 6(2017/8)
        )
        
        jet_btag = (
                event.Jet.btagDeepFlavB > btag_id(
                    self.btag_wp, 
                    self._era + 'APV' if self._isAPV else self._era
                )
        )
        
        #good_jets = jets[~jet_btag & jet_mask]
        #no replacing the jets from the jet collection
        good_jets = jets[jet_mask]
        #print("good_jets",good_jets[:20])
        good_bjet = jets[jet_btag & jet_mask & (np.abs(jets.eta)<2.4)]
        
        ngood_jets  = ak.num(good_jets)
        ngood_bjets = ak.num(good_bjet)
        
        event['ngood_bjets'] = ngood_bjets
        event['ngood_jets']  = ngood_jets
       
        # lepton quantities
        if(self._isnotpho):
            def z_lepton_pair(leptons):
                pair = ak.combinations(leptons, 2, axis=1, fields=['l1', 'l2'])
                mass = (pair.l1 + pair.l2).mass
                cand = ak.local_index(mass, axis=1) == ak.argmin(np.abs(mass - self.zmass), axis=1)

                extra_lepton = leptons[(
                    ~ak.any(leptons.metric_table(pair[cand].l1) <= 0.01, axis=2) & 
                    ~ak.any(leptons.metric_table(pair[cand].l2) <= 0.01, axis=2) )
                                   ]
                return pair[cand], extra_lepton, cand
        
            dilep, extra_lep, z_cand_mask = z_lepton_pair(tight_lep)
            lead_lep = ak.firsts(ak.where(dilep.l1.pt >  dilep.l2.pt, dilep.l1, dilep.l2),axis=1)
            subl_lep = ak.firsts(ak.where(dilep.l1.pt <= dilep.l2.pt, dilep.l1, dilep.l2),axis=1)
        
            dilep_p4 = (lead_lep + subl_lep)
        else:
            dilep_p4 = lead_photon

            #need to ffix it extra lep why we need it in the caluculation off met variables just put the dummy values for now 
            extra_lep = loose_lep
            lead_lep = ak.firsts(dd_photons)
            subl_lep = ak.firsts(dd_photons)
            
        dilep_m  = dilep_p4.mass
        dilep_pt = dilep_p4.pt
        dilep_eta = dilep_p4.eta
        dilep_phi = dilep_p4.phi
        #deltaphi Z+jets and met



        # high level observables
        p4_met = ak.zip(
            {
                "pt": event.MET.pt,
                "eta": ak.zeros_like(event.MET.pt),
                "phi": event.MET.phi,
                "mass": ak.zeros_like(event.MET.pt),
                "charge": ak.zeros_like(event.MET.pt),
            },
            with_name="PtEtaPhiMCandidate",
            behavior=candidate.behavior,
        )

        
        emu_met = ak.firsts(extra_lep, axis=1) + p4_met
	
        reco_met_pt = ak.where(ntight_lep==3,emu_met.pt, p4_met.pt)
        reco_met_phi = ak.where(ntight_lep==3,emu_met.phi, p4_met.phi)

	
	    # this definition is not correct as it doesn't include the mass of the second Z
        dilep_et_ll = np.sqrt(dilep_pt**2 + dilep_m**2)
        dilep_et_met = np.sqrt(reco_met_pt**2 + self.zmass**2)
        dilep_mt = ak.where(
                ntight_lep==3,
                np.sqrt((dilep_et_ll + dilep_et_met)**2 - (dilep_p4.pvec + emu_met.pvec).p2),
                np.sqrt((dilep_et_ll + dilep_et_met)**2 - (dilep_p4.pvec +  p4_met.pvec).p2)
	    )
	
        # dilep_dphi = lead_lep.delta_phi(subl_lep)
        # dilep_deta = np.abs(lead_lep.eta - subl_lep.eta)
        # dilep_dR   = lead_lep.delta_r(subl_lep)
        dilep_dphi_met  = ak.where(ntight_lep==3,dilep_p4.delta_phi(emu_met), dilep_p4.delta_phi(p4_met))
        #scalar_balance = ak.where(ntight_lep==3, emu_met.pt/dilep_p4.pt, p4_met.pt/dilep_p4.pt)
        

        
        # 2jet and vbs related variables
        lead_jet = ak.firsts(good_jets)
        subl_jet = ak.firsts(good_jets[lead_jet.delta_r(good_jets)>0.01])
        third_jet = ak.firsts(good_jets[(lead_jet.delta_r(good_jets)>0.01) & (subl_jet.delta_r(good_jets)>0.01)])
        

        zjets = ak.where(ngood_jets >=3, lead_jet + subl_jet+third_jet+dilep_p4, lead_jet + subl_jet+dilep_p4)
        
        dijet_mass = (lead_jet + subl_jet).mass
        dijet_deta = np.abs(lead_jet.eta - subl_jet.eta)
        event['dijet_mass'] = dijet_mass
        event['dijet_deta'] = dijet_deta 
        #dijet_zep1 = np.abs(2*lead_lep.eta - (lead_jet.eta + subl_jet.eta))/dijet_deta
        #dijet_zep2 = np.abs(2*subl_lep.eta - (lead_jet.eta + subl_jet.eta))/dijet_deta
        
        #hhotspot 
        if is_data:
            hotspot = ~((dilep_eta>=1.48) & (dilep_eta<=1.58) & (dilep_phi>= -0.78) & (dilep_phi <= -0.55))
            endcap_ph_beamhalo = ~((np.abs(dilep_eta)>1.58) & ( (np.abs(dilep_phi) > 2.87979083333) |  (np.abs(dilep_phi) < 0.26179916666 )) )
        else :
            hotspot = np.ones(len(event), dtype='bool')
            endcap_ph_beamhalo = np.ones(len(event), dtype='bool')
            


        #hem_veto
        if '2018' in self._era :
            hem_veto = ((good_jets.phi > -1.57) & (good_jets.phi < -0.87) & (good_jets.eta > -3.00) & (good_jets.eta < -1.40))
            if is_data:
                _runid = ((event.run >= 319077) & (event.run <= 325175))
                jet_mask_hm = (ak.any(hem_veto,axis=-1)) & (_runid) 
            else :
                jet_mask_hm = ((ak.any(hem_veto,axis=-1)) & (np.random.uniform(size=len(event)) <= 0.647 ))

        else :    
            jet_mask_hm = np.zeros(len(event), dtype='bool') 
            
        
        print("jet_mask_hm",jet_mask_hm)
         
        selection.add("jet_mask_hm", ~jet_mask_hm)        
        selection.add("ph_hsbh",ak.fill_none((hotspot & endcap_ph_beamhalo),False))
        
        delta_phi_Zjets_met  = np.abs(zjets.delta_phi(p4_met))

        min_dphi_met_j = ak.min(np.abs(
            ak.where(
                ntight_lep==3, 
                good_jets.delta_phi(emu_met), 
                good_jets.delta_phi(p4_met)
            )
        ), axis=1)

        event['min_dphi_met_j'] = min_dphi_met_j
        event['delta_phi_Zjets_met'] = delta_phi_Zjets_met
        # define basic selection
        selection.add(
            "require-ossf",
            (ntight_lep==2) & (nloose_lep==0) &
            (ak.firsts(tight_lep).pt>25) &
            ak.fill_none((lead_lep.pdgId + subl_lep.pdgId)==0, False)
        )
        
        selection.add(
            "require-osof",
            (ntight_lep==2) & (nloose_lep==0) &
            (ak.firsts(tight_lep).pt>25) &
           ak.fill_none(np.abs(lead_lep.pdgId) != np.abs(subl_lep.pdgId), False)
        )
        
        selection.add(
            "require-3lep",
            (ntight_lep==3) & (nloose_lep==0) &
            (ak.firsts(tight_lep).pt>25) &
            ak.fill_none((lead_lep.pdgId + subl_lep.pdgId)==0, False)
        )
        
        selection.add(
            "require-4lep",
            (ntight_lep>=2) & (nloose_lep + ntight_lep)==4 &
            (ak.firsts(tight_lep).pt>25) &
            ak.fill_none((lead_lep.pdgId + subl_lep.pdgId)==0, False)
        )
        selection.add(
            'met_pt' ,
            ak.where(
                ngood_jets<2,
                ak.fill_none(reco_met_pt > 100, False),
                ak.fill_none(reco_met_pt > 120, False)
            )
        )
        selection.add('low_met_pt', ak.fill_none((reco_met_pt < 100) & (reco_met_pt > 50), False))
        selection.add('dilep_m'   , ak.fill_none(np.abs(dilep_m - self.zmass) < 15, False))
        selection.add('dilep_m_50', ak.fill_none(dilep_m > 50, False))
        selection.add(
            'dilep_pt',
            ak.where(
                selection.require(**{"require-3lep":True}),
                ak.fill_none(dilep_pt>45, False),
                ak.fill_none(dilep_pt>60, False)
            )
        )
        selection.add('dilep_eta', ak.fill_none(np.abs(dilep_eta)<2.5, False))
        selection.add("dilep_dphi_met", ak.fill_none(np.abs(dilep_dphi_met)>1.0, False))
        selection.add(
            "min_dphi_met_j",
            ak.where(
                ngood_jets <= 1, 
                ak.fill_none(np.abs(min_dphi_met_j)>0.25, False), 
                ak.fill_none(np.abs(min_dphi_met_j)>0.5, False), 
            )
        )
        selection.add("delta_phi_Zjets_met", ak.fill_none(delta_phi_Zjets_met > 2.5 ,False))


        #zero lep
        selection.add('0lep_inph', (nextra_lep_inph ==0))
        selection.add('0lep', (ntight_lep==0))
        selection.add('1pho', ak.fill_none((ntight_pho==1) & (nloose_pho==0) & (ak.firsts(tight_photons).pt>55),False))
        selection.add('met_0'        , ak.fill_none(p4_met.pt > 0, False))
        

        # jet demography
        selection.add('1njets' , ngood_jets  >= 1 )
        selection.add('2njets' , ngood_jets  >= 2 )
        selection.add('1nbjets', ngood_bjets >= 1 )
        selection.add('0bjets', ngood_bjets == 0 )
        selection.add('0nhtaus', nhtaus_lep  == 0 )
        selection.add('0nhtausph', nhtaus_ph  == 0 )
        
        selection.add('dijet_deta', ak.fill_none(dijet_deta > 2.5, False))
        selection.add('dijet_mass_400' , ak.fill_none(dijet_mass >  400, False))
        selection.add('dijet_mass_800' , ak.fill_none(dijet_mass >  800, False))
        selection.add('dijet_mass_1200', ak.fill_none(dijet_mass > 1200, False))

        #print("met_pt", reco_met_pt)
        # Define all variables for the GNN
        event['met_pt'  ] = reco_met_pt
        event['met_phi' ] = reco_met_phi
        event['dilep_mt'] = dilep_mt
        event['dilep_m'] = dilep_m
        event['dilep_pt'] = dilep_pt
        event['dilep_eta'] = dilep_eta
        event['njets'   ] = ngood_jets
        event['bjets'   ] = ngood_bjets
        event['dphi_met_ll'] = dilep_dphi_met
        event['dijet_mass'] = dijet_mass
        event['dijet_deta'] = dijet_deta
        event['min_dphi_met_j'] = min_dphi_met_j

        event['leading_lep_pt'  ] = lead_lep.pt
        event['leading_lep_eta' ] = lead_lep.eta
        event['leading_lep_phi' ] = lead_lep.phi
        event['trailing_lep_pt' ] = subl_lep.pt
        event['trailing_lep_eta'] = subl_lep.eta
        event['trailing_lep_phi'] = subl_lep.phi
                
        event['lead_jet_pt'  ] = lead_jet.pt
        event['lead_jet_eta' ] = lead_jet.eta
        event['lead_jet_phi' ] = lead_jet.phi
        event['trail_jet_pt' ] = subl_jet.pt
        event['trail_jet_eta'] = subl_jet.eta
        event['trail_jet_phi'] = subl_jet.phi
        event['third_jet_pt' ] = third_jet.pt
        event['third_jet_eta'] = third_jet.eta
        event['third_jet_phi'] = third_jet.phi
        
        # Apply GNN
        event['gnn_score'] = applyGNN(event).get_nnscore()
        event['gnn_flat'] = self.gnn_flat_fnc(event['gnn_score'])

        # Now adding weights
        if not is_data:
            weights.add('genweight', event.genWeight)
            self._btag.append_btag_sf(jets, weights)
            self._purw.append_pileup_weight(weights, event.Pileup.nPU)
            
            if self._isnotpho:
                self._add_trigger_sf(weights, lead_lep, subl_lep)
                weights.add (
                            'LeptonSF', 
                            lead_lep.SF*subl_lep.SF, 
                            lead_lep.SF_up*subl_lep.SF_up, 
                            lead_lep.SF_down*subl_lep.SF_down
                )

            
            _ones = np.ones(len(weights.weight()))

            
            if ((self.ewk_process_name) and (self.ewk_process_name != "GJets")) :
                self.ewk_corr.get_weight(
                    event.GenPart,
                    event.Generator.x1,
                    event.Generator.x2,
                    weights
                )
            else:
                weights.add("kEW", _ones, _ones, _ones)
            

            
            if "PSWeight" in event.fields:
                theory_ps_weight(weights, event.PSWeight)
            else:
                theory_ps_weight(weights, None)
            if "LHEPdfWeight" in event.fields:
                theory_pdf_weight(weights, event.LHEPdfWeight)
            else:
                theory_pdf_weight(weights, None)
             
            if ('LHEScaleWeight' in event.fields) and (len(event.LHEScaleWeight[0]) > 0):
                weights.add('QCDScale0w'  , _ones, event.LHEScaleWeight[:, 1], event.LHEScaleWeight[:, 7])
                weights.add('QCDScale1w'  , _ones, event.LHEScaleWeight[:, 3], event.LHEScaleWeight[:, 5])
                weights.add('QCDScale2w'  , _ones, event.LHEScaleWeight[:, 0], event.LHEScaleWeight[:, 8])
               
            if 'LHEReweightingWeight' in event.fields and 'aQGC' in dataset:
                for i in range(1057):
                    weights.add(f"eft_{self._eftnames[i]}", event.LHEReweightingWeight[:, i])
  
            if 'L1PreFiringWeight' in event.fields:
                weights.add("prefiring_weight", event.L1PreFiringWeight.Nom, event.L1PreFiringWeight.Dn, event.L1PreFiringWeight.Up)         
                  
        if not self._isnotpho:
            if is_data:
                weights.add('prescaleweight', event.prescaleweight)
            else :
                weights.add('PhotonSF',self._phSF,self._phSF+self._phSFerr,self._phSF-self._phSFerr)
                if self.ewk_process_name == "GJets" :
                    gjets_wt = ak.where(gjetsnnlo > 1, gjetsnnlo,1)
                    weights.add("gnnlo",gjets_wt,gjets_wt,gjets_wt)
                     #weights.add("gnnlo",gjets_wt)

        # selections
        #common_sel = ['lumimask', 'metfilter']
        #print("monika commom sel is reduced and only 2jets applied 1 bjet and other cuts and deltaphi")
        common_sel = ['triggers', 'lumimask', 'metfilter']
        channels = {
            '''
            # inclusive regions
            "cat-SR0J": common_sel + [
		    'require-ossf', 'dilep_m', 'dilep_pt', '0nhtaus',
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'met_pt', '~1nbjets', 
		    "~1njets" # 0 jets
	    ], 
            "cat-SR1J": common_sel + [
		    'require-ossf', 'dilep_m', 'dilep_pt', '0nhtaus', 
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'met_pt', '~1nbjets', 
		    "1njets", "~2njets" # 1 jet selection
	    ],
            "cat-SR2J": common_sel + [
		    'require-ossf', 'dilep_m', 'dilep_pt', '0nhtaus', 
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'met_pt', '~1nbjets', 
		    "2njets" # more that 2 jets
	    ], 
            "cat-DY": common_sel + [
		    'require-ossf', 'dilep_m', 'dilep_pt', '0nhtaus', 
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'low_met_pt', # between 50 to 100 GeV
		    '~1nbjets', "~2njets" # low jet mutiplicity below 2 jets
	    ], 
            "cat-3L": common_sel + [
		    'require-3lep', 'dilep_m', 'dilep_pt',
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'met_pt', '~1nbjets', "~2njets" 
	    ],
            "cat-EM": common_sel + [
		    'require-osof', 'dilep_m', 'dilep_pt',
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'met_pt', '~1nbjets', "~2njets"],
            "cat-TT": common_sel + [
		    'require-osof', 'dilep_m', 'dilep_pt', 
		    'dilep_dphi_met', 
		    # 'min_dphi_met_j',
		    'met_pt', '1nbjets', "~2njets"
	    ],
            "cat-NR": common_sel + [
		    'require-osof', '~dilep_m', 'dilep_pt',
		    'dilep_dphi_met', 'min_dphi_met_j',  
		    'met_pt', '1nbjets', "~2njets"
	    ],
            # vector boson scattering
            "vbs-SR": common_sel + [
		    'require-ossf', 'dilep_m', 'dilep_pt', '0nhtaus',
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'met_pt', '~1nbjets', 
		    "2njets", "dijet_deta", "dijet_mass_400"
	    ],
            "vbs-SR0": common_sel + [
		    'require-ossf', 'dilep_m', 'dilep_pt', '0nhtaus',
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'met_pt', '~1nbjets', 
		    "2njets", "dijet_deta", "dijet_mass_400", "~dijet_mass_800"
	    ],
            "vbs-SR1": common_sel + [
		    'require-ossf', 'dilep_m', 'dilep_pt', '0nhtaus',
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'met_pt', '~1nbjets', 
		    "2njets", "dijet_deta", "dijet_mass_800", "~dijet_mass_1200"
	    ],
            "vbs-SR2": common_sel + [
		    'require-ossf', 'dilep_m', 'dilep_pt', 
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'met_pt', '~1nbjets', '0nhtaus',
		    "2njets", "dijet_deta", "dijet_mass_1200",
	    ],
            "vbs-DY": common_sel + [
		    'dijet_deta','require-ossf', 'dilep_m', 'dilep_pt',
		    'dilep_dphi_met', 'min_dphi_met_j', 
		    'low_met_pt', '~1nbjets', '0nhtaus', 
		    "2njets", "~dijet_mass_400"],
            "vbs-3L": common_sel + [
		    'require-3lep', 'dilep_m', 'dilep_pt',
		    'dilep_dphi_met', 'min_dphi_met_j',
		    'met_pt', '~1nbjets', "2njets"
	    ],
            "vbs-EM": common_sel + [
		    'require-osof', 'dilep_m', 'dilep_pt', 
		    'dilep_dphi_met', 'min_dphi_met_j',
		    'met_pt', '~1nbjets',"2njets"],
            "vbs-TT": common_sel + [
		    'require-osof', 'dilep_m', 'dilep_pt', 
		    'dilep_dphi_met', 'min_dphi_met_j',
		    'met_pt', '1nbjets', "2njets"
	    ],
            "vbs-NR": common_sel + [                
		    'require-osof', '~dilep_m', 'dilep_pt',
		    'dilep_dphi_met', 'min_dphi_met_j',
		    'met_pt', '1nbjets', "2njets"
	    ],
            '''
           "photon_VBS":common_sel + [
               '1pho','2njets','0bjets',
               '0nhtausph','0lep_inph','dilep_pt','dilep_eta',
               'met_0','dilep_dphi_met',
               'min_dphi_met_j',#'delta_phi_Zjets_met',
               'dijet_deta','dijet_mass_400' ],
            "photon_VBS_1":common_sel + [
                '1pho','2njets','0bjets',
                '0nhtausph','0lep_inph','dilep_pt','dilep_eta',
                'met_0','dilep_dphi_met','jet_mask_hm',
                'min_dphi_met_j',#'delta_phi_Zjets_met',
                'dijet_deta','dijet_mass_400' ],
            "photon_VBS_2":common_sel + [
                '1pho','2njets','0bjets',
                '0nhtausph','0lep_inph','dilep_pt','dilep_eta',
                'met_0','dilep_dphi_met','ph_hsbh',
                'min_dphi_met_j',#'delta_phi_Zjets_met',
                'dijet_deta','dijet_mass_400'
            ],   
           "photon_VBS_3":common_sel + [
               '1pho','2njets','0bjets',
               '0nhtausph','0lep_inph','dilep_pt','dilep_eta',
               'met_0','dilep_dphi_met',
               'min_dphi_met_j','delta_phi_Zjets_met',
               'dijet_deta','dijet_mass_400'
               
           ],


           "photon_VBS_4":common_sel + [
               '1pho','2njets','0bjets',
               '0nhtausph','0lep_inph','dilep_pt','dilep_eta',
               'met_0','dilep_dphi_met','ph_hsbh','jet_mask_hm',
               'min_dphi_met_j','delta_phi_Zjets_met',
               'dijet_deta','dijet_mass_400'

           ],    

        }

        if shift_name is None:
            systematics = [None] + list(weights.variations)
        else:
            systematics = [shift_name]
            
        def _format_variable(variable, cut):
            if cut is None:
                return ak.to_numpy(ak.fill_none(variable, np.nan))
            else:
                return ak.to_numpy(ak.fill_none(variable[cut], np.nan))
        
        def _histogram_filler(ch, syst, var, _weight=None):
            sel_ = channels[ch]
            sel_args_ = {
                s.replace('~',''): (False if '~' in s else True) for s in sel_ if var not in s
            }
            cut =  selection.require(**sel_args_)

            systname = 'nominal' if syst is None else syst
            
            if _weight is None: 
                if syst in weights.variations:
                    weight = weights.weight(modifier=syst)[cut]
                else:
                    weight = weights.weight()[cut]
            else:
                weight = weights.weight()[cut] * _weight[cut]

            histos[var].fill(
                **{
                    "channel": ch, 
                    "systematic": systname, 
                    var: _format_variable(event[var], cut), 
                    "weight": weight,
                }
            )
            
        def _gnn_dumper(ch):
            sel_ = channels[ch]
            sel_args_ = {
                s.replace('~',''): (False if '~' in s else True) for s in sel_
            }
            cut =  selection.require(**sel_args_)
            weight = weights.weight()[cut]
            
            _dicv = {
                ch: {
                    "gnn": _format_variable(event["gnn_score"], cut).tolist(), 
                    "weight": weight.tolist()
                }
            }
            if 'gnn_dump' in histos:
                histos["gnn_dump"].update(_dicv)
            else:
                histos["gnn_dump"] = _dicv
            
        for ch in channels:
            if self.dump_gnn_array:
                _gnn_dumper(ch)
            for sys in systematics:
                _histogram_filler(ch, sys, 'met_pt')
                _histogram_filler(ch, sys, 'dilep_mt')
                _histogram_filler(ch, sys, 'dilep_pt')
                _histogram_filler(ch, sys, 'dilep_eta')
                _histogram_filler(ch, sys, 'dilep_m')
                _histogram_filler(ch, sys, 'njets')
                _histogram_filler(ch, sys, 'bjets')
                _histogram_filler(ch, sys, 'dphi_met_ll')
                _histogram_filler(ch, sys, 'dijet_mass')
                _histogram_filler(ch, sys, 'dijet_deta')
                _histogram_filler(ch, sys, 'lead_jet_pt')
                _histogram_filler(ch, sys, 'trail_jet_pt')
                _histogram_filler(ch, sys, 'lead_jet_eta')
                _histogram_filler(ch, sys, 'trail_jet_eta')
                _histogram_filler(ch, sys, 'min_dphi_met_j')
                _histogram_filler(ch, sys, 'dijet_mass')
                _histogram_filler(ch, sys, 'gnn_score')
                _histogram_filler(ch, sys, 'gnn_flat')
                _histogram_filler(ch, sys, 'delta_phi_Zjets_met')
               # _histogram_filler(ch, sys, 'dijet_deta')
               # _histogram_filler(ch, sys, 'lead_jet_pt')
               # _histogram_filler(ch, sys, 'trail_jet_pt')
               # _histogram_filler(ch, sys, 'lead_jet_eta')
               # _histogram_filler(ch, sys, 'trail_jet_eta')
               # _histogram_filler(ch, sys, 'min_dphi_met_j')

        return {dataset: histos}
        
    def process(self, event: processor.LazyDataFrame):
        dataset_name = event.metadata['dataset']
        is_data = event.metadata.get("is_data")

        if '2015' in self._era:        
            print(" i am here")
            jets = event.Jet
            hem_veto = ((jets.phi > -1.57) & (jets.phi < -0.87) & (jets.eta > -2.50) & (jets.eta < -1.30))
            if is_data:
                _runid = (event.run >= 319077)
                j_mask = ak.where(hem_veto & _runid, 0.001, 1)
            else :
                j_mask = ak.where((hem_veto) & (np.random.uniform(size=len(jets)) > 0.85), 0.001, 1  )

            #print(j_mask)     
            met = event.MET
            # HEM15/16 issue
            #_runid = (event.run >= 319077)
            #jets = event.Jet
            #j_mask = ak.where((jets.phi > -1.57) & (jets.phi < -0.87) &
            #                  (jets.eta > -2.50) & (jets.eta <  1.30) & 
            #                  _runid, 0.8, 1)
            #event['met_pt'] = met.pt
            #event['met_phi'] = met.phi            
            jets['pt']   = jets.pt
            jets['mass'] = jets.mass
            event = ak.with_field(event, jets, 'Jet')
        
                
        return self.process_shift(event, None)
        
        # x-y met shit corrections
        # for the moment I am replacing the met with the corrected met 
        # before doing the JES/JER corrections
        
        run = event.run 
        npv = event.PV.npvs
        met = event.MET
        
        met = met_phi_xy_correction(
            event.MET, run, npv, 
            is_mc=not is_data, 
            era=self._era
        )
        event = ak.with_field(event, met, 'MET')
		
        # Adding scale factors to Muon and Electron fields
        muon = event.Muon 
        electron = event.Electron
        muonSF_nom, muonSF_up, muonSF_down = self._leSF.muonSF(muon)
        elecSF_nom, elecSF_up, elecSF_down = self._leSF.electronSF(electron)
        
        muon['SF'] = muonSF_nom
        muon['SF_up'] = muonSF_up
        muon['SF_down'] = muonSF_down

        electron['SF'] = elecSF_nom
        electron['SF_up'] = elecSF_up
        electron['SF_down'] = elecSF_down

        event = ak.with_field(event, muon, 'Muon')
        event = ak.with_field(event, electron, 'Electron')

        # JES/JER corrections
        jets = self._jmeu.corrected_jets(event.Jet, event.fixedGridRhoFastjetAll, event.caches[0])
        met  = self._jmeu.corrected_met(event.MET, jets, event.fixedGridRhoFastjetAll, event.caches[0])
         
        # Apply rochester_correction
        muon=event.Muon
        muonEnUp=event.Muon
        muonEnDown=event.Muon
        muon_pt,muon_pt_roccorUp,muon_pt_roccorDown=rochester_correction(is_data).apply_rochester_correction (muon)
        
        muon['pt'] = muon_pt
        muonEnUp['pt'] = muon_pt_roccorUp
        muonEnDown['pt'] = muon_pt_roccorDown 
        event = ak.with_field(event, muon, 'Muon')
        
        # Electron corrections
        electronEnUp=event.Electron
        electronEnDown=event.Electron

        electronEnUp  ['pt'] = event.Electron['pt'] + event.Electron.energyErr/np.cosh(event.Electron.eta)
        electronEnDown['pt'] = event.Electron['pt'] - event.Electron.energyErr/np.cosh(event.Electron.eta)	
	
        # define all the shifts
        shifts = [
            # Jets
            ({"Jet": jets               , "MET": met               }, None     ),
            ({"Jet": jets.JES_Total.up  , "MET": met.JES_Total.up  }, "JESUp"  ),
            ({"Jet": jets.JES_Total.down, "MET": met.JES_Total.down}, "JESDown"),
            ({"Jet": jets.JER.up        , "MET": met.JER.up        }, "JERUp"  ),
            ({"Jet": jets.JER.down      , "MET": met.JER.down      }, "JERDown"),
            ({"Jet": jets, "MET": met.MET_UnclusteredEnergy.up     }, "UESUp"  ),
            ({"Jet": jets, "MET": met.MET_UnclusteredEnergy.down   }, "UESDown"), 
            
            # Electrons + MET shift (FIXME: shift to be added)
            ({"Electron": electronEnUp  }, "ElectronEnUp"  ),
            ({"Electron": electronEnDown}, "ElectronEnDown"),
            # Muon + MET shifts
            ({"Muon": muonEnUp  }, "MuonRocUp"),
            ({"Muon": muonEnDown}, "MuonRocDown"),
        ]
        
        shifts = [
            self.process_shift(
                update_collection(event, collections), 
                name
            ) for collections, name in shifts
        ]
        return processor.accumulate(shifts)
    
    def postprocess(self, accumulator):
        return accumulator
